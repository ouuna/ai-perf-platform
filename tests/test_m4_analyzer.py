"""M4 测试：Analyzer 规则层 + 数字一致性校验器 + AI 层。"""

from __future__ import annotations

import pytest

from app.analyzer.ai import AIAnalysis, AIAnalyzer
from app.analyzer.consistency import NumberConsistencyError, validate_numbers
from app.analyzer.rules import (
    AnalysisResult,
    Fact,
    detect_anomalies,
    detect_knee,
    detect_resource_correlation,
    evaluate_sla,
)
from app.llm.client import MockLLM
from app.planner.models import SLA


def _make_rows() -> list[dict]:
    """构造模拟 metric_points 行（接口 + 聚合）。"""
    rows = []
    # 20 秒阶梯：1-10 用户，吞吐递增到峰值后延迟上升
    for i in range(20):
        users = min(1 + i // 2, 10)
        # 前 15 秒吞吐增长，后 5 秒吞吐停滞 + 延迟上升
        if i < 15:
            rps = users * 10.0
            p95 = 50.0 + users * 2
        else:
            rps = 100.0  # 吞吐封顶
            p95 = 200.0 + (i - 15) * 50  # 延迟持续上升
        rows.append(
            {
                "interface": None,
                "ts": float(i),
                "users": float(users),
                "rps": rps,
                "p50_ms": p95 * 0.5,
                "p95_ms": p95,
                "p99_ms": p95 * 1.5,
                "error_rate": 0.0,
                "error_count": 0,
                "cpu_percent": 90.0 + (i % 3),  # 持续高 CPU，触发饱和
                "mem_percent": 30.0 + i * 2,
            }
        )
        rows.append(
            {
                "interface": "/products",
                "ts": float(i),
                "users": float(users),
                "rps": rps * 0.8,
                "p50_ms": p95 * 0.5,
                "p95_ms": p95,
                "p99_ms": p95 * 1.5,
                "error_rate": 0.0,
                "error_count": 0,
                "cpu_percent": 90.0 + (i % 3),  # 持续高 CPU，触发饱和
                "mem_percent": 30.0 + i * 2,
            }
        )
    return rows


def _rows_to_sqlite_rows(rows: list[dict]):
    """把 dict 列表转成类似 sqlite3.Row 的对象（支持 [] 访问）。"""

    class FakeRow:
        def __init__(self, d: dict):
            self._d = d

        def __getitem__(self, key):
            return self._d[key]

        def get(self, key, default=None):
            return self._d.get(key, default)

    return [FakeRow(r) for r in rows]


# ---- 拐点检测 ----


def test_detect_knee_finds_stable_users() -> None:
    rows = _rows_to_sqlite_rows(_make_rows())
    users, rps = detect_knee(rows)
    assert users is not None
    assert rps is not None
    # 吞吐峰值 100，拐点应在吞吐接近峰值且延迟上升处
    assert rps >= 90
    assert users <= 10


# ---- SLA 判定 ----


def test_evaluate_sla_pass() -> None:
    rows = _rows_to_sqlite_rows(_make_rows())
    sla = SLA(p95_ms=10000, max_error_rate=0.5)
    result = evaluate_sla(rows, sla)
    # 所有接口 P95 < 10000，错误率 0，应全部通过
    assert all(result.values())


def test_evaluate_sla_fail_on_p95() -> None:
    rows = _rows_to_sqlite_rows(_make_rows())
    sla = SLA(p95_ms=100, max_error_rate=0.5)  # P95 峰值 > 100
    result = evaluate_sla(rows, sla)
    # 有接口 P95 超过 100，应存在未通过
    assert not all(result.values())


# ---- 异常模式 ----


def test_detect_anomalies_latency_growth() -> None:
    rows = _rows_to_sqlite_rows(_make_rows())
    facts = detect_anomalies(rows)
    kinds = {f.id for f in facts}
    # 延迟持续上涨，应检测到
    assert any("延迟" in f.statement for f in facts)
    assert len(kinds) > 0


def test_detect_anomalies_empty_on_short_series() -> None:
    rows = _rows_to_sqlite_rows(_make_rows()[:4])
    facts = detect_anomalies(rows)
    # 采样点太少，不产生异常
    assert facts == []


# ---- 资源相关性 ----


def test_detect_resource_cpu_saturation() -> None:
    rows = _rows_to_sqlite_rows(_make_rows())
    facts = detect_resource_correlation(rows)
    # CPU 平均接近饱和（后半段 95%），应检测到
    assert any("CPU" in f.statement for f in facts)


def test_detect_resource_memory_growth() -> None:
    rows = _rows_to_sqlite_rows(_make_rows())
    facts = detect_resource_correlation(rows)
    # 内存持续增长（30 到 68），应检测到
    assert any("内存" in f.statement for f in facts)


# ---- 数字一致性校验器 ----


def _facts_with_pool() -> AnalysisResult:
    result = AnalysisResult()
    result.facts = [
        Fact(
            id="F1",
            kind="knee",
            statement="吞吐拐点",
            values={"max_stable_users": 10.0, "capacity_rps": 100.0},
        ),
        Fact(id="F2", kind="sla", statement="P95 未达标", values={"p95_ms": 500.0}),
    ]
    return result


def test_validate_numbers_pass_when_traceable() -> None:
    result = _facts_with_pool()
    # 引用的 10、100、500 都能在事实清单找到
    validate_numbers("最大稳定并发是 10，容量 100 RPS，P95 是 500ms", result)


def test_validate_numbers_raises_on_fabricated() -> None:
    result = _facts_with_pool()
    # 引用不存在的 9999
    with pytest.raises(NumberConsistencyError) as exc_info:
        validate_numbers("吞吐达到 9999 RPS", result)
    assert "9999" in str(exc_info.value)


def test_validate_numbers_tolerance() -> None:
    result = _facts_with_pool()
    # 501 与 500 误差 0.2%，在 5% 容忍内，应通过
    validate_numbers("P95 是 501ms", result)


# ---- AI 层 ----


def _ai_response() -> str:
    import json

    return json.dumps(
        {
            "hypotheses": [
                {
                    "statement": "CPU 饱和导致吞吐封顶在 100 RPS，最大稳定并发约 10",
                    "confidence": "high",
                    "evidence_refs": ["F1", "F2"],
                }
            ],
            "investigation_suggestions": ["检查 CPU 密集逻辑"],
            "optimization_suggestions": ["优化计算"],
            "limitations": ["数据量较小"],
        }
    )


def test_ai_analyzer_success() -> None:
    result = _facts_with_pool()
    rows = _rows_to_sqlite_rows(_make_rows())
    mock = MockLLM(responses=[_ai_response()])
    analysis = AIAnalyzer(mock).analyze(result, rows)
    assert isinstance(analysis, AIAnalysis)
    assert len(analysis.hypotheses) == 1
    assert analysis.hypotheses[0].confidence == "high"


def test_ai_analyzer_rejects_fabricated_number() -> None:
    """AI 引用了不存在的数字，应触发重试，最终用尽重试报错。"""
    import json

    result = _facts_with_pool()
    rows = _rows_to_sqlite_rows(_make_rows())
    fabricated = json.dumps(
        {
            "hypotheses": [
                {
                    "statement": "吞吐达到 9999 RPS，远超预期",
                    "confidence": "high",
                    "evidence_refs": ["F1"],
                }
            ],
            "investigation_suggestions": [],
            "optimization_suggestions": [],
            "limitations": [],
        }
    )
    mock = MockLLM(responses=[fabricated] * 5)  # 一直编造数字
    from app.analyzer.ai import AnalyzerError

    with pytest.raises(AnalyzerError):
        AIAnalyzer(mock, max_retries=3).analyze(result, rows)


def test_ai_analyzer_retries_then_succeeds() -> None:
    """第一次编造数字，第二次正确，应重试成功。"""
    import json

    result = _facts_with_pool()
    rows = _rows_to_sqlite_rows(_make_rows())
    bad = json.dumps(
        {
            "hypotheses": [
                {"statement": "吞吐 9999", "confidence": "high", "evidence_refs": ["F1"]}
            ],
            "investigation_suggestions": [],
            "optimization_suggestions": [],
            "limitations": [],
        }
    )
    mock = MockLLM(responses=[bad, _ai_response()])
    analysis = AIAnalyzer(mock, max_retries=3).analyze(result, rows)
    assert len(analysis.hypotheses) == 1
