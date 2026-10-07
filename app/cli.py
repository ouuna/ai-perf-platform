"""CLI 入口（typer）：完整性能测试流程的命令行接口。"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Annotated

import typer

from app.db import Database
from app.llm.client import create_llm_client
from app.planner.models import TestPlan
from app.planner.planner import Planner

app = typer.Typer(help="AI 智能性能测试平台 CLI", no_args_is_help=True)


@app.command()
def version() -> None:
    """显示版本。"""
    from app import __version__

    typer.echo(f"ai-perf-platform {__version__}")


@app.command()
def plan(
    requirement: Annotated[str, typer.Argument(help="自然语言测试需求")],
    output: Annotated[Path, typer.Option("--output", "-o", help="方案输出 JSON 路径")] = Path(
        "plan.json"
    ),
    provider: Annotated[
        str, typer.Option("--provider", help="LLM provider (mock/anthropic/openai)")
    ] = "mock",
) -> None:
    """自然语言需求 → 结构化测试方案。"""
    client = create_llm_client(provider)
    plan = Planner(client).generate(requirement)
    output.write_text(plan.model_dump_json(indent=2), encoding="utf-8")
    typer.echo(f"方案已生成: {output}")
    typer.echo(plan.model_dump_json(indent=2))


@app.command()
def run(
    plan_file: Annotated[Path, typer.Argument(help="方案 JSON 文件路径")],
    target_url: Annotated[str, typer.Option("--target", help="目标地址")] = "http://127.0.0.1:8001",
    output_dir: Annotated[Path, typer.Option("--output", "-o", help="报告输出目录")] = Path(
        "examples/reports"
    ),
) -> None:
    """执行压测 + 分析 + 生成报告（同步）。"""
    from app.analyzer.ai import AIAnalyzer
    from app.analyzer.rules import run_rule_analysis
    from app.collector.collector import HostMonitor, collect_from_run, save_points
    from app.reporter.reporter import ReportContext, Reporter, _line_chart_svg
    from app.runner.runner import Runner

    plan = TestPlan.model_validate_json(plan_file.read_text(encoding="utf-8"))

    db = Database()
    task_id = db.create_task(plan_file.stem, status="running")

    monitor = HostMonitor(interval=1.0)
    monitor.start()

    csv_dir = tempfile.mkdtemp(prefix="cli_")
    result = Runner(plan=plan, target_url=target_url, csv_dir=csv_dir).run()
    monitor.stop()

    if result.returncode != 0 and not result.stopped_by_sla:
        typer.echo(f"压测失败: {result.stdout[-500:]}", err=True)
        raise typer.Exit(code=1)

    points = collect_from_run(task_id, csv_dir, monitor.samples)
    save_points(points, db=db)

    rows = db.get_metric_points(task_id)
    rule_result = run_rule_analysis(task_id, plan, db)

    ai = None
    try:
        ai = AIAnalyzer(create_llm_client()).analyze(rule_result, rows)
    except Exception:  # noqa: BLE001
        ai = None

    iface_names = sorted({r["interface"] for r in rows if r["interface"]})
    metric_summary = []
    for name in ["Aggregated"] + iface_names:
        subset = [r for r in rows if (r["interface"] or "Aggregated") == name]
        if not subset:
            continue
        rps = sum(float(r["rps"] or 0) for r in subset) / len(subset)
        p95 = sum(float(r["p95_ms"] or 0) for r in subset) / len(subset)
        p99 = sum(float(r["p99_ms"] or 0) for r in subset) / len(subset)
        p50 = sum(float(r["p50_ms"] or 0) for r in subset) / len(subset)
        err = sum(float(r["error_rate"] or 0) for r in subset) / len(subset)
        metric_summary.append(
            {
                "name": "整体" if name == "Aggregated" else name,
                "rps": rps,
                "p50": p50,
                "p95": p95,
                "p99": p99,
                "error_rate": err,
            }
        )

    agg_rows = sorted([r for r in rows if r["interface"] is None], key=lambda r: float(r["ts"]))
    rps_series = [
        {
            "name": "RPS",
            "points": [(float(r["ts"]), float(r["rps"] or 0)) for r in agg_rows],
            "color": "#378ADD",
        },
        {
            "name": "P95 (ms)",
            "points": [(float(r["ts"]), float(r["p95_ms"] or 0)) for r in agg_rows],
            "color": "#D85A30",
        },
    ]
    trend_svg = _line_chart_svg("吞吐与 P95 延迟趋势", rps_series)
    host_note = (
        f"压测机自监控：采样 {len(monitor.samples)} 次，"
        f"CPU 峰值 {max((s[1] for s in monitor.samples), default=0):.1f}%"
    )

    ctx = ReportContext(
        requirement=plan_file.stem,
        plan=plan,
        result=rule_result,
        ai=ai,
        metric_summary=metric_summary,
        trend_svg=trend_svg,
        host_monitor_note=host_note,
    )
    paths = Reporter().save(ctx, output_dir)
    typer.echo(f"报告已生成: {paths['html']}")
    typer.echo(f"报告已生成: {paths['markdown']}")
    db.close()


@app.command()
def serve(
    host: Annotated[str, typer.Option("--host", help="监听地址")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", help="监听端口")] = 8000,
) -> None:
    """启动 Web 服务（FastAPI + 前端）。"""
    import uvicorn

    typer.echo(f"启动服务: http://{host}:{port}")
    uvicorn.run("app.main:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    app()
