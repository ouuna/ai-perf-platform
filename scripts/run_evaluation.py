"""评测：验证平台对 6 种注入故障的瓶颈定位能力。

评测设计（诚实原则）：
1. 每种故障有"标准答案"（故障类型）。
2. 评测分两个层次：
   - 层次 A「问题识别」：规则层是否识别出"存在性能问题"及问题方向（延迟/吞吐/资源）。
   - 层次 B「精确定位」：规则层/AI 是否能精确定位到"具体故障类型"（如慢查询 vs N+1）。
3. 所有数字来自真实运行，不夸大、不粉饰。定位不准确就如实记录。

产出 docs/eval/results.json。
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

import httpx

TARGET_URL = "http://127.0.0.1:8001"

# 故障名 -> 精确故障关键词（层次 B 判定用）
FAULT_PRECISE_KEYWORDS: dict[str, list[str]] = {
    "slow_query": ["慢查询", "全表扫描", "无索引", "slow"],
    "n_plus_1": ["n+1", "n_plus_1", "多次查询", "逐条"],
    "lock_contention": ["锁", "lock", "竞争", "串行", "contention"],
    "pool_exhaustion": ["连接池", "pool", "耗尽", "连接"],
    "memory_leak": ["内存", "泄漏", "memory", "leak"],
    "cpu_bound": ["cpu", "计算密集", "哈希", "密集"],
}

# 故障名 -> 期望的"问题方向"信号（层次 A 判定用）
FAULT_DIRECTION: dict[str, str] = {
    "slow_query": "延迟",
    "n_plus_1": "延迟",
    "lock_contention": "吞吐",
    "pool_exhaustion": "延迟",
    "memory_leak": "内存",
    "cpu_bound": "cpu",
}

# 每种故障对应的压测目标接口
FAULT_ENDPOINTS: dict[str, str] = {
    "slow_query": "/products",
    "n_plus_1": "/orders/1",
    "lock_contention": "/orders",
    "pool_exhaustion": "/pool-query",
    "memory_leak": "/cache-fill",
    "cpu_bound": "/cpu-work",
}


def _wait_target() -> bool:
    for _ in range(20):
        try:
            httpx.get(f"{TARGET_URL}/health", timeout=1)
            return True
        except httpx.HTTPError:
            time.sleep(0.5)
    return False


def _set_fault(name: str, enabled: bool) -> None:
    httpx.post(f"{TARGET_URL}/admin/faults", json={"name": name, "enabled": enabled})


def _disable_all() -> None:
    for name in FAULT_PRECISE_KEYWORDS:
        _set_fault(name, False)


def _run_pressure(fault: str) -> tuple[list, dict]:
    """跑一次压测，返回 (规则事实 statements, AI 结果 dict)。"""
    from app.analyzer.ai import AIAnalyzer
    from app.analyzer.rules import run_rule_analysis
    from app.collector.collector import HostMonitor, collect_from_run, save_points
    from app.db import Database
    from app.llm.client import MockLLM
    from app.planner.models import TestPlan
    from app.runner.runner import Runner

    endpoint = FAULT_ENDPOINTS[fault]
    method = "POST" if fault == "lock_contention" else "GET"
    params = {"product_id": "1", "quantity": "1"} if fault == "lock_contention" else {}

    plan = TestPlan.model_validate(
        {
            "test_type": "load",
            "target_interfaces": [
                {
                    # 用中性接口名，避免接口名本身含故障关键词污染评测判定
                    "name": "endpoint", "method": method, "path": endpoint,
                    "params": params, "weight": 1, "depends_on": None, "assert_status": 200,
                }
            ],
            "load_model": {
                "start_users": 1, "step_users": 3, "step_duration_seconds": 3,
                "max_users": 12, "spawn_rate": 2, "think_time_seconds": 0.0,
            },
            "sla": {"p95_ms": 5000, "max_error_rate": 0.5},
        }
    )

    db = Database("data/eval.db")
    task_id = db.create_task(f"eval-{fault}", status="running")

    monitor = HostMonitor(interval=0.5)
    monitor.start()
    csv_dir = tempfile.mkdtemp(prefix=f"eval_{fault}_")
    Runner(plan=plan, target_url=TARGET_URL, csv_dir=csv_dir,
           sla_error_rate_break=0.9, timeout_seconds=180).run()
    monitor.stop()

    points = collect_from_run(task_id, csv_dir, monitor.samples)
    save_points(points, db=db)

    rows = db.get_metric_points(task_id)
    rule_result = run_rule_analysis(task_id, plan, db)

    # AI 解读：用 MockLLM，返回一个"引用事实清单"的通用解读。
    # 由于 mock 无法真正理解故障，这里 AI 的定位能力取决于事实清单是否含精确信号。
    ai_hypotheses: list[str] = []
    ai_facts_refs: list[str] = []
    try:
        # 让 AI 基于事实清单生成解读（mock 返回引用 F1 的假设）
        mock_resp = _mock_ai_response(rule_result.fact_ids())
        ai = AIAnalyzer(MockLLM(responses=[mock_resp])).analyze(rule_result, rows)
        ai_hypotheses = [h.statement for h in ai.hypotheses]
        ai_facts_refs = [ref for h in ai.hypotheses for ref in h.evidence_refs]
    except Exception as exc:  # noqa: BLE001
        ai_hypotheses = [f"AI 解读失败: {exc}"]

    db.close()

    ai = {"hypotheses": ai_hypotheses, "evidence_refs": ai_facts_refs}
    return [f.statement for f in rule_result.facts], ai


def _mock_ai_response(fact_ids: list[str]) -> str:
    """生成一个引用事实编号的 mock AI 解读（数字可追溯）。"""
    ref = fact_ids[0] if fact_ids else "F1"
    return json.dumps(
        {
            "hypotheses": [
                {
                    "statement": f"规则层识别到性能问题（见 {ref}），需进一步定位具体原因",
                    "confidence": "low",
                    "evidence_refs": [ref],
                }
            ],
            "investigation_suggestions": ["进一步分析具体瓶颈"],
            "optimization_suggestions": [],
            "limitations": ["mock 解读，非真实 AI"],
        }
    )


def _hit_precise(statements: list[str], keywords: list[str]) -> bool:
    text = " ".join(statements).lower()
    return any(kw.lower() in text for kw in keywords)


def _hit_direction(statements: list[str], direction: str) -> bool:
    """判断是否识别出问题方向（延迟/吞吐/内存/cpu）。"""
    text = " ".join(statements).lower()
    direction_map = {
        "延迟": ["延迟", "响应时间", "latency", "慢"],
        "吞吐": ["吞吐", "rps", "throughput", "锯齿", "封顶"],
        "内存": ["内存", "memory", "泄漏"],
        "cpu": ["cpu", "计算", "饱和"],
    }
    return any(kw in text for kw in direction_map.get(direction, []))


def run_evaluation() -> dict:
    if not _wait_target():
        return {"error": "靶站未启动"}

    results: dict[str, dict] = {}
    total = len(FAULT_PRECISE_KEYWORDS)
    direction_hit = 0
    precise_hit = 0
    ai_hit = 0

    for fault in FAULT_PRECISE_KEYWORDS:
        print(f"评测故障: {fault} ...")
        _disable_all()
        _set_fault(fault, True)

        try:
            rule_statements, ai = _run_pressure(fault)
        except Exception as exc:  # noqa: BLE001
            results[fault] = {"error": str(exc)}
            continue

        dir_hit = _hit_direction(rule_statements, FAULT_DIRECTION[fault])
        prec_hit = _hit_precise(rule_statements, FAULT_PRECISE_KEYWORDS[fault])
        ai_prec = _hit_precise(ai["hypotheses"], FAULT_PRECISE_KEYWORDS[fault])

        if dir_hit:
            direction_hit += 1
        if prec_hit:
            precise_hit += 1
        if ai_prec:
            ai_hit += 1

        results[fault] = {
            "direction": FAULT_DIRECTION[fault],
            "rule_facts": rule_statements,
            "ai_hypotheses": ai["hypotheses"],
            "direction_hit": dir_hit,
            "precise_hit": prec_hit,
            "ai_hit": ai_prec,
        }
        print(f"  方向命中: {dir_hit}, 精确定位: {prec_hit}, AI 定位: {ai_prec}")

    _disable_all()

    return {
        "total": total,
        "direction_accuracy": direction_hit / total if total else 0,
        "precise_accuracy": precise_hit / total if total else 0,
        "ai_accuracy": ai_hit / total if total else 0,
        "results": results,
    }


def main() -> int:
    output = Path("docs/eval")
    output.mkdir(parents=True, exist_ok=True)
    summary = run_evaluation()
    (output / "results.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("\n=== 评测结果 ===")
    print(json.dumps(
        {k: v for k, v in summary.items() if k != "results"},
        ensure_ascii=False, indent=2,
    ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
