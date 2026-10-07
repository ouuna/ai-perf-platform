"""M3 测试：Collector CSV 解析与资源对齐、Runner 错误率计算逻辑。"""

from __future__ import annotations

import csv
from pathlib import Path

from app.collector.collector import (
    MetricPoint,
    _safe_float,
    _safe_int,
    collect_from_run,
    parse_stats_history,
)
from app.runner.runner import Runner


def _write_stats_history(tmp_path: Path) -> Path:
    """写一个模拟的 stats_history CSV。"""
    path = tmp_path / "stats_stats_history.csv"
    header = [
        "Timestamp", "User Count", "Type", "Name", "Request Count", "Failure Count",
        "Median Response Time", "Average Response Time", "Min Response Time",
        "Max Response Time", "Average Content Size", "Requests/s", "Failures/s",
        "50%", "66%", "75%", "80%", "90%", "95%", "98%", "99%", "99.9%", "99.99%", "100%",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        # 2 秒，每接口 1 秒一行 + Aggregated
        for ts in (1.0, 2.0):
            w.writerow([ts, 10, "GET", "list_products", 100, 2, "50", "55", "10",
                        "200", "100", "50", "1", "50", "52", "55", "58", "60", "70", "80",
                        "90", "95", "99", "200"])
            w.writerow([ts, 10, "", "Aggregated", 200, 4, "55", "60", "10", "300",
                        "120", "100", "2", "55", "58", "60", "65", "70", "80", "90",
                        "100", "105", "110", "300"])
    return path


def test_parse_stats_history(tmp_path: Path) -> None:
    path = _write_stats_history(tmp_path)
    rows = parse_stats_history(path)
    assert len(rows) == 4  # 2 秒 × 2 行


def test_collect_from_run_aligns_resources(tmp_path: Path) -> None:
    _write_stats_history(tmp_path)
    # 压测机资源样本：ts=1, cpu=20, mem=100; ts=2, cpu=40, mem=120
    host_samples = [(1.0, 20.0, 100.0), (2.0, 40.0, 120.0)]
    points = collect_from_run(task_id=1, csv_dir=tmp_path, host_samples=host_samples)
    assert len(points) == 4

    # Aggregated 行 interface 为 None
    agg = [p for p in points if p.interface is None]
    assert len(agg) == 2

    # 第一秒资源对齐
    first = points[0]
    assert first.cpu_percent == 20.0
    assert first.mem_percent == 100.0
    assert first.error_rate == 0.02  # 2/100


def test_collect_error_rate_zero_when_no_requests(tmp_path: Path) -> None:
    path = tmp_path / "stats_stats_history.csv"
    header = ["Timestamp", "User Count", "Type", "Name", "Request Count", "Failure Count",
              "Requests/s", "Failures/s", "50%", "95%", "99%"]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerow([1.0, 0, "", "Aggregated", 0, 0, "0", "0", "0", "0", "0"])
    points = collect_from_run(1, tmp_path)
    assert points[0].error_rate == 0.0


def test_safe_float_handles_na() -> None:
    assert _safe_float("N/A") is None
    assert _safe_float("") is None
    assert _safe_float("12.5") == 12.5


def test_safe_int_handles_na() -> None:
    assert _safe_int("N/A") is None
    assert _safe_int("3") == 3


def test_metric_point_to_dict() -> None:
    p = MetricPoint(task_id=1, interface="/x", ts=1.0, rps=10.0)
    d = p.to_dict()
    assert d["task_id"] == 1
    assert d["interface"] == "/x"
    assert d["rps"] == 10.0
    assert d["p95_ms"] is None


def test_runner_build_cmd(tmp_path: Path) -> None:
    """验证 Runner 命令构造包含关键参数。"""
    from app.planner.models import TestPlan

    plan = TestPlan.model_validate(
        {
            "test_type": "load",
            "target_interfaces": [
                {"name": "p", "method": "GET", "path": "/p", "weight": 1}
            ],
            "load_model": {
                "start_users": 1, "step_users": 1, "step_duration_seconds": 10,
                "max_users": 10, "spawn_rate": 2, "think_time_seconds": 0.5,
            },
            "sla": {},
        }
    )
    runner = Runner(plan=plan, target_url="http://localhost:8001", csv_dir=tmp_path)
    cmd = runner._build_cmd(tmp_path / "locustfile.py")
    joined = " ".join(cmd)
    assert "--headless" in joined
    assert "--csv-full-history" in joined
    assert "-u" in cmd
    assert "-r" in cmd
