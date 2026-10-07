"""M6 测试：API 路由（用 MockLLM，不依赖真实压测）。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def _client() -> TestClient:
    return TestClient(app)


def test_health() -> None:
    resp = _client().get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_create_task() -> None:
    resp = _client().post(
        "/api/tasks",
        json={"requirement": "压测登录接口", "target_url": "http://127.0.0.1:8001"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["task_id"] > 0
    assert data["status"] in {"planning", "created"}


def test_get_task_not_found() -> None:
    resp = _client().get("/api/tasks/999999")
    assert resp.status_code == 404


def test_list_tasks() -> None:
    resp = _client().get("/api/tasks")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_run_task_without_plan() -> None:
    # 创建一个任务但先生成方案前就 run，应 400
    resp = _client().post(
        "/api/tasks",
        json={"requirement": "压测", "target_url": "http://127.0.0.1:8001"},
    )
    task_id = resp.json()["task_id"]
    # 直接 run（plan 还没生成完，可能没有 plan）
    resp2 = _client().post(f"/api/tasks/{task_id}/run")
    # 可能 400（无方案）或 200（后台已生成），都算合理，只验证不 500
    assert resp2.status_code in {200, 400}


def test_report_before_complete() -> None:
    resp = _client().post(
        "/api/tasks",
        json={"requirement": "压测", "target_url": "http://127.0.0.1:8001"},
    )
    task_id = resp.json()["task_id"]
    resp2 = _client().get(f"/api/tasks/{task_id}/report")
    # 未完成，应 400
    assert resp2.status_code in {400, 404}


def test_index_returns_html() -> None:
    resp = _client().get("/")
    assert resp.status_code == 200
    assert "性能测试" in resp.text
    assert "<html" in resp.text
