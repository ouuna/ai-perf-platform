"""M5 测试：Reporter 报告生成（HTML + Markdown）。"""

from __future__ import annotations

from app.analyzer.ai import AIAnalysis
from app.analyzer.rules import AnalysisResult, Fact
from app.planner.models import TestPlan
from app.reporter.reporter import ReportContext, Reporter, _line_chart_svg


def _plan() -> TestPlan:
    return TestPlan.model_validate(
        {
            "test_type": "load",
            "target_interfaces": [
                {"name": "list_products", "method": "GET", "path": "/products", "weight": 1}
            ],
            "load_model": {
                "start_users": 1,
                "step_users": 10,
                "step_duration_seconds": 60,
                "max_users": 100,
                "spawn_rate": 5,
                "think_time_seconds": 1.0,
            },
            "sla": {"p95_ms": 500, "max_error_rate": 0.01},
        }
    )


def _result() -> AnalysisResult:
    r = AnalysisResult()
    r.max_stable_users = 50.0
    r.estimated_capacity_rps = 480.5
    r.sla_pass = {"list_products": True, "Aggregated": True}
    r.facts = [
        Fact(
            id="F1",
            kind="knee",
            statement="吞吐拐点",
            values={"max_stable_users": 50.0, "capacity_rps": 480.5},
        ),
        Fact(id="F2", kind="sla", statement="SLA 通过", values={"pass": 1.0}),
    ]
    return r


def _ai() -> AIAnalysis:
    return AIAnalysis.model_validate(
        {
            "hypotheses": [
                {
                    "statement": "CPU 饱和导致吞吐封顶在 480 RPS",
                    "confidence": "high",
                    "evidence_refs": ["F1"],
                }
            ],
            "investigation_suggestions": ["检查 CPU 密集逻辑"],
            "optimization_suggestions": ["优化计算"],
            "limitations": ["数据量较小"],
        }
    )


def _ctx() -> ReportContext:
    return ReportContext(
        requirement="对商品列表接口做负载测试",
        plan=_plan(),
        result=_result(),
        ai=_ai(),
        metric_summary=[
            {
                "name": "list_products",
                "rps": 480.5,
                "p50": 50.0,
                "p95": 120.0,
                "p99": 200.0,
                "error_rate": 0.001,
            },
        ],
        trend_svg=_line_chart_svg(
            "吞吐趋势",
            [{"name": "RPS", "points": [(0, 100), (1, 200), (2, 300)], "color": "#378ADD"}],
        ),
        host_monitor_note="压测机 CPU 峰值 30%，非瓶颈",
    )


def test_line_chart_svg_generates() -> None:
    svg = _line_chart_svg(
        "测试",
        [{"name": "RPS", "points": [(0, 100), (1, 200)], "color": "#378ADD"}],
    )
    assert "<svg" in svg
    assert "测试" in svg
    assert "RPS" in svg


def test_line_chart_svg_empty_series() -> None:
    assert _line_chart_svg("空", []) == ""


def test_render_html_contains_sections() -> None:
    html = Reporter().render_html(_ctx())
    assert "性能测试报告" in html
    assert "测试目的与范围" in html
    assert "拐点与容量结论" in html
    assert "瓶颈分析" in html
    assert "list_products" in html


def test_render_html_sla_pass_markup() -> None:
    html = Reporter().render_html(_ctx())
    assert "通过" in html
    assert 'class="pass"' in html


def test_render_markdown_contains_sections() -> None:
    md = Reporter().render_markdown(_ctx())
    assert "# 性能测试报告" in md
    assert "## 1. 测试目的与范围" in md
    assert "## 5. 拐点与容量结论" in md
    assert "**50**" in md  # 最大稳定并发


def test_render_html_hypothesis_confidence() -> None:
    html = Reporter().render_html(_ctx())
    assert "置信度" in html
    assert "CPU 饱和导致吞吐封顶在 480 RPS" in html


def test_save_reports(tmp_path) -> None:
    paths = Reporter().save(_ctx(), tmp_path)
    assert (tmp_path / "report.html").exists()
    assert (tmp_path / "report.md").exists()
    assert paths["html"].name == "report.html"
    assert paths["markdown"].name == "report.md"


def test_render_without_ai() -> None:
    ctx = _ctx()
    ctx.ai = None
    html = Reporter().render_html(ctx)
    assert "未启用 AI 解读" in html
