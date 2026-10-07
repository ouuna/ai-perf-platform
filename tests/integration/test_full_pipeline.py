"""集成测试：MockLLM 跑通「需求 → 报告」全链路（对靶站，短时低并发）。

这些测试需要真实靶站运行，标记为 integration，在 CI 的 integration job 中执行。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest

from app.collector.collector import HostMonitor, collect_from_run, save_points
from app.llm.client import MockLLM
from app.planner.models import TestPlan
from app.planner.planner import Planner
from app.runner.runner import Runner

TARGET_URL = "http://127.0.0.1:8001"


def _wait_target() -> None:
    for _ in range(20):
        try:
            httpx.get(f"{TARGET_URL}/health", timeout=1)
            return
        except httpx.HTTPError:
            time.sleep(0.5)
    raise RuntimeError("靶站未启动")


def _valid_plan_json() -> str:
    return json.dumps(
        {
            "test_type": "load",
            "target_interfaces": [
                {"name": "login", "method": "POST", "path": "/login",
                 "params": {"username": "tester"}, "weight": 1, "depends_on": None,
                 "assert_status": 200},
                {"name": "list_products", "method": "GET", "path": "/products",
                 "params": {}, "weight": 3, "depends_on": "login", "assert_status": 200},
            ],
            "load_model": {"start_users": 1, "step_users": 2, "step_duration_seconds": 2,
                           "max_users": 3, "spawn_rate": 1, "think_time_seconds": 0.1},
            "sla": {"p95_ms": 5000, "p99_ms": 10000, "max_error_rate": 0.5, "min_rps": None},
            "preconditions": [], "notes": "",
        }
    )


@pytest.mark.integration
def test_full_pipeline_mock(tmp_path: Path) -> None:
    """需求 → 方案 → 脚本 → 压测 → 采集 → 入库（短时低并发）。"""
    _wait_target()

    mock = MockLLM(responses=[_valid_plan_json()])
    plan = Planner(mock).generate("对登录和商品列表做短时低并发压测")
    assert isinstance(plan, TestPlan)

    monitor = HostMonitor(interval=0.5)
    monitor.start()

    runner = Runner(plan=plan, target_url=TARGET_URL, csv_dir=tmp_path,
                    sla_error_rate_break=0.9, timeout_seconds=60)
    result = runner.run()
    monitor.stop()

    assert result.returncode == 0, f"locust 失败: {result.stderr[-500:]}"
    assert not result.stopped_by_sla

    # 采集
    points = collect_from_run(task_id=1, csv_dir=tmp_path, host_samples=monitor.samples)
    assert len(points) > 0, "未采集到任何指标点"

    # 入库
    save_points(points)

    # 有聚合指标
    agg = [p for p in points if p.interface is None]
    assert len(agg) > 0
    # 有接口指标
    iface = [p for p in points if p.interface == "list_products"]
    assert len(iface) > 0
