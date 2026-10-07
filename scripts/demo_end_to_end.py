"""端到端 demo 脚本：需求 → 方案 → 压测 → 采集 → 分析 → 报告。

用法：python scripts/demo_end_to_end.py
（需要靶站在 127.0.0.1:8001 运行）

默认用 MockLLM，无需 API Key。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from app.analyzer.ai import AIAnalyzer
from app.analyzer.rules import run_rule_analysis
from app.collector.collector import HostMonitor, collect_from_run, save_points
from app.db import Database
from app.llm.client import MockLLM
from app.planner.planner import Planner
from app.reporter.reporter import ReportContext, Reporter, _line_chart_svg
from app.runner.runner import Runner

TARGET_URL = "http://127.0.0.1:8001"

# Mock 的 AI 解读（瓶颈假设基于事实清单，数字可追溯）
_MOCK_ANALYSIS = json.dumps(
    {
        "hypotheses": [
            {
                "statement": "吞吐在并发上升后趋于平稳，符合阶梯加压下的典型容量曲线",
                "confidence": "medium",
                "evidence_refs": [],
            }
        ],
        "investigation_suggestions": ["观察更高并发下的延迟与错误率变化"],
        "optimization_suggestions": ["针对高延迟接口做性能剖析"],
        "limitations": ["压测时间较短，容量结论需更长稳定期验证"],
    }
)

_VALID_PLAN = json.dumps(
    {
        "test_type": "load",
        "target_interfaces": [
            {"name": "login", "method": "POST", "path": "/login",
             "params": {"username": "demo"}, "weight": 1, "depends_on": None,
             "assert_status": 200},
            {"name": "list_products", "method": "GET", "path": "/products",
             "params": {}, "weight": 3, "depends_on": "login", "assert_status": 200},
            {"name": "product_detail", "method": "GET", "path": "/products/1",
             "params": {}, "weight": 2, "depends_on": "login", "assert_status": 200},
        ],
        "load_model": {"start_users": 1, "step_users": 2, "step_duration_seconds": 3,
                       "max_users": 5, "spawn_rate": 1, "think_time_seconds": 0.1},
        "sla": {"p95_ms": 1000, "p99_ms": 2000, "max_error_rate": 0.05, "min_rps": None},
        "preconditions": [], "notes": "端到端 demo",
    }
)


def main() -> int:
    output_dir = Path("examples/reports")
    output_dir.mkdir(parents=True, exist_ok=True)

    print("== 1. Planner：需求 → 方案 ==")
    mock = MockLLM(responses=[_VALID_PLAN])
    plan = Planner(mock).generate("对登录、商品列表、商品详情做短时低并发压测")
    print(f"   测试类型: {plan.test_type.value}, 接口数: {len(plan.target_interfaces)}")

    db = Database("data/demo.db")

    print("== 2. Runner：执行压测 ==")
    monitor = HostMonitor(interval=0.5)
    monitor.start()
    csv_dir = tempfile.mkdtemp(prefix="demo_")
    runner = Runner(plan=plan, target_url=TARGET_URL, csv_dir=csv_dir,
                    sla_error_rate_break=0.5, timeout_seconds=120)
    result = runner.run()
    monitor.stop()
    print(f"   returncode: {result.returncode}, 时长: {result.duration_seconds:.1f}s")

    if result.returncode != 0:
        print("   压测失败，日志尾部：")
        print(result.stdout[-500:])
        return 1

    print("== 3. Collector：采集指标 ==")
    task_id = db.create_task("端到端 demo 压测")
    points = collect_from_run(task_id=task_id, csv_dir=csv_dir, host_samples=monitor.samples)
    save_points(points, db=db)
    print(f"   采集 {len(points)} 个指标点")

    rows = db.get_metric_points(task_id)

    print("== 4. Analyzer：规则 + AI 解读 ==")
    rule_result = run_rule_analysis(task_id, plan, db)
    print(f"   事实清单 {len(rule_result.facts)} 条")
    for f in rule_result.facts:
        print(f"   [{f.id}] {f.statement}")

    ai_mock = MockLLM(responses=[_MOCK_ANALYSIS])
    try:
        ai = AIAnalyzer(ai_mock).analyze(rule_result, rows)
        print(f"   AI 瓶颈假设 {len(ai.hypotheses)} 条")
    except Exception as exc:  # noqa: BLE001
        print(f"   AI 解读跳过: {exc}")
        ai = None

    print("== 5. Reporter：生成报告 ==")
    # 构建指标摘要
    iface_names = sorted({r["interface"] for r in rows if r["interface"]})
    metric_summary = []
    for name in ["Aggregated"] + iface_names:
        subset = [r for r in rows if (r["interface"] or "Aggregated") == name]
        if not subset:
            continue
        rps = sum(float(r["rps"] or 0) for r in subset) / len(subset)
        p50 = sum(float(r["p50_ms"] or 0) for r in subset) / len(subset)
        p95 = sum(float(r["p95_ms"] or 0) for r in subset) / len(subset)
        p99 = sum(float(r["p99_ms"] or 0) for r in subset) / len(subset)
        err = sum(float(r["error_rate"] or 0) for r in subset) / len(subset)
        metric_summary.append({
            "name": "整体" if name == "Aggregated" else name,
            "rps": rps, "p50": p50, "p95": p95, "p99": p99, "error_rate": err,
        })

    # 趋势图：用聚合行
    agg_rows = [r for r in rows if r["interface"] is None]
    agg_rows.sort(key=lambda r: float(r["ts"]))
    rps_points = [(float(r["ts"]), float(r["rps"] or 0)) for r in agg_rows]
    p95_points = [(float(r["ts"]), float(r["p95_ms"] or 0)) for r in agg_rows]
    trend_svg = _line_chart_svg(
        "吞吐与 P95 延迟趋势",
        [
            {"name": "RPS", "points": rps_points, "color": "#378ADD"},
            {"name": "P95 (ms)", "points": p95_points, "color": "#D85A30"},
        ],
    )

    host_note = (
        f"压测机自监控：采样 {len(monitor.samples)} 次，"
        f"CPU 峰值 {max((s[1] for s in monitor.samples), default=0):.1f}%，"
        f"内存峰值 {max((s[2] for s in monitor.samples), default=0):.1f}MB"
    )

    ctx = ReportContext(
        requirement="对登录、商品列表、商品详情做短时低并发压测（demo）",
        plan=plan,
        result=rule_result,
        ai=ai,
        metric_summary=metric_summary,
        trend_svg=trend_svg,
        host_monitor_note=host_note,
    )

    paths = Reporter().save(ctx, output_dir)
    print(f"   报告已生成: {paths['html']}")
    print(f"   报告已生成: {paths['markdown']}")

    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
