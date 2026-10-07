"""FastAPI 路由：任务提交、方案查看、压测执行、报告获取。

任务流程（异步）：
1. POST /api/tasks 提交需求 → 后台生成 TestPlan
2. GET /api/tasks/{id} 查看任务状态与方案
3. POST /api/tasks/{id}/run 触发压测（后台执行）
4. GET /api/tasks/{id}/report 获取报告（HTML 或 markdown）

所有 LLM 相关默认用 MockLLM（无 Key 可跑），有 Key 则用真实 provider。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, Field

from app.db import get_db
from app.llm.client import create_llm_client
from app.planner.models import TestPlan
from app.planner.planner import Planner, PlannerError
from app.safety import SafetyError, validate_request

router = APIRouter(prefix="/api", tags=["tasks"])


class TaskCreateRequest(BaseModel):
    requirement: str = Field(description="自然语言测试需求")
    target_url: str = Field(default="http://127.0.0.1:8001", description="目标地址")
    confirmed_limits: bool = Field(default=False, description="是否已确认超限参数")


class TaskCreateResponse(BaseModel):
    task_id: int
    status: str


# ---------------------------------------------------------------------------
# 后台执行
# ---------------------------------------------------------------------------


def _plan_task(task_id: int, requirement: str) -> None:
    """后台生成方案。"""
    db = get_db()
    try:
        client = create_llm_client()
        plan = Planner(client).generate(requirement)
        db.save_plan(task_id, plan.model_dump())
        db.update_task_status(task_id, "planned")
    except (PlannerError, Exception) as exc:  # noqa: BLE001
        db.update_task_status(task_id, "failed")
        # 记录错误到 notes
        db.execute(
            "UPDATE tasks SET requirement = requirement || ? WHERE id = ?",
            (f" [错误: {exc}]", task_id),
        )


def _run_task(task_id: int, target_url: str) -> None:
    """后台执行压测 + 采集 + 分析 + 报告。"""
    import tempfile

    from app.analyzer.ai import AIAnalyzer
    from app.analyzer.rules import run_rule_analysis
    from app.collector.collector import HostMonitor, collect_from_run, save_points
    from app.reporter.reporter import ReportContext, Reporter, _line_chart_svg
    from app.runner.runner import Runner

    db = get_db()
    db.update_task_status(task_id, "running")

    try:
        plan_data = db.get_plan(task_id)
        if plan_data is None:
            raise RuntimeError("未找到方案，请先生成方案")
        plan = TestPlan.model_validate(plan_data)

        # Safety 校验
        task = db.get_task(task_id)
        # 从 requirement 里无法可靠解析 users，这里用 plan 的 max_users
        duration = plan.load_model.step_duration_seconds * 10
        validate_request(target_url, plan.load_model.max_users, duration)

        monitor = HostMonitor(interval=1.0)
        monitor.start()

        csv_dir = tempfile.mkdtemp(prefix=f"task{task_id}_")
        runner = Runner(plan=plan, target_url=target_url, csv_dir=csv_dir)
        result = runner.run()
        monitor.stop()

        if result.returncode != 0 and result.stopped_by_sla is False:
            raise RuntimeError(f"压测失败: {result.stderr or result.stdout[-500:]}")

        # 采集
        points = collect_from_run(task_id, csv_dir, monitor.samples)
        save_points(points, db=db)

        # 分析
        rows = db.get_metric_points(task_id)
        rule_result = run_rule_analysis(task_id, plan, db)

        ai = None
        try:
            ai_client = create_llm_client()
            ai = AIAnalyzer(ai_client).analyze(rule_result, rows)
        except Exception:  # noqa: BLE001 - AI 解读失败不阻断报告生成
            ai = None

        # 报告
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
            f"CPU 峰值 {max((s[1] for s in monitor.samples), default=0):.1f}%，"
            f"内存峰值 {max((s[2] for s in monitor.samples), default=0):.1f}MB"
        )

        ctx = ReportContext(
            requirement=task["requirement"],
            plan=plan,
            result=rule_result,
            ai=ai,
            metric_summary=metric_summary,
            trend_svg=trend_svg,
            host_monitor_note=host_note,
        )
        reporter = Reporter()
        db.save_report(task_id, "html", reporter.render_html(ctx))
        db.save_report(task_id, "markdown", reporter.render_markdown(ctx))
        db.update_task_status(task_id, "completed")

    except (SafetyError, RuntimeError, Exception) as exc:  # noqa: BLE001
        db.update_task_status(task_id, "failed")
        db.execute(
            "UPDATE tasks SET requirement = requirement || ? WHERE id = ?",
            (f" [错误: {exc}]", task_id),
        )


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------


@router.post("/tasks", response_model=TaskCreateResponse)
def create_task(req: TaskCreateRequest, background: BackgroundTasks) -> TaskCreateResponse:
    """提交需求，后台生成方案。"""
    db = get_db()
    task_id = db.create_task(req.requirement, status="planning")
    background.add_task(_plan_task, task_id, req.requirement)
    return TaskCreateResponse(task_id=task_id, status="planning")


@router.get("/tasks/{task_id}")
def get_task(task_id: int) -> dict[str, Any]:
    """查看任务状态与方案。"""
    db = get_db()
    task = db.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    plan = db.get_plan(task_id)
    return {
        "id": task_id,
        "requirement": task["requirement"],
        "status": task["status"],
        "plan": plan,
        "created_at": task["created_at"],
    }


@router.get("/tasks")
def list_tasks() -> list[dict[str, Any]]:
    """列出所有任务。"""
    db = get_db()
    rows = db.query("SELECT id, requirement, status, created_at FROM tasks ORDER BY id DESC")
    return [dict(r) for r in rows]


@router.post("/tasks/{task_id}/run")
def run_task(task_id: int, background: BackgroundTasks) -> dict[str, str]:
    """触发压测（后台执行）。"""
    db = get_db()
    task = db.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if db.get_plan(task_id) is None:
        raise HTTPException(status_code=400, detail="请先生成方案")

    # 从方案提取 target_url（demo 固定靶站）
    target_url = "http://127.0.0.1:8001"
    background.add_task(_run_task, task_id, target_url)
    return {"status": "running"}


@router.get("/tasks/{task_id}/report")
def get_report(task_id: int, fmt: str = "html") -> dict[str, Any]:
    """获取报告（html 或 markdown）。"""
    db = get_db()
    task = db.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task["status"] != "completed":
        raise HTTPException(status_code=400, detail=f"任务尚未完成（当前状态: {task['status']}）")

    content = db.get_report(task_id, fmt)
    if content is None:
        raise HTTPException(status_code=404, detail="报告不存在")
    return {"task_id": task_id, "format": fmt, "content": content}


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
