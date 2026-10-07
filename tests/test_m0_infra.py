"""M0 基础设施测试：LLM 抽象层、Safety、数据库。"""

from __future__ import annotations

import pytest

from app.db import Database
from app.llm.client import (
    LLMError,
    LLMMessage,
    MockLLM,
    create_llm_client,
)
from app.safety import SafetyError, check_limits, check_target

# ---- LLM 抽象层 ----


def test_mock_llm_returns_sequential_responses() -> None:
    mock = MockLLM(responses=["one", "two"])
    r1 = mock.complete([LLMMessage(role="user", content="hi")])
    r2 = mock.complete([LLMMessage(role="user", content="hi")])
    assert r1.text == "one"
    assert r2.text == "two"
    assert r1.model == "mock"


def test_mock_llm_exhausts_to_default() -> None:
    mock = MockLLM(responses=["only"])
    mock.complete([])
    r2 = mock.complete([])
    assert r2.text == '{"message": "mock"}'


def test_create_llm_client_defaults_to_mock() -> None:
    client = create_llm_client("mock")
    assert isinstance(client, MockLLM)


def test_create_llm_client_unknown_provider_raises() -> None:
    with pytest.raises(LLMError):
        create_llm_client("not-a-provider")


def test_anthropic_client_requires_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    from app.llm.client import AnthropicClient

    with pytest.raises(LLMError):
        AnthropicClient("", "model")


# ---- Safety ----


def test_check_target_allows_localhost() -> None:
    assert check_target("http://localhost:8000").ok
    assert check_target("http://127.0.0.1:8080/login").ok


def test_check_target_blocks_external_host() -> None:
    result = check_target("https://example.com")
    assert not result.ok
    assert "白名单" in result.reason


def test_check_limits_within_bounds() -> None:
    assert check_limits(users=100, duration_seconds=60).ok


def test_check_limits_exceeds_users_requires_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AP_MAX_USERS", "2000")
    result = check_limits(users=5000, duration_seconds=60)
    assert not result.ok
    assert result.require_confirmation

    # 显式确认后放行
    confirmed = check_limits(users=5000, duration_seconds=60, confirmed=True)
    assert confirmed.ok


def test_validate_request_raises_on_blocked_target() -> None:
    with pytest.raises(SafetyError):
        from app.safety import validate_request

        validate_request("https://evil.com", 100, 60)


# ---- 数据库 ----


def test_db_task_and_plan_roundtrip(tmp_path: str) -> None:
    db = Database(str(tmp_path / "test.db"))
    task_id = db.create_task("压测登录接口", status="created")
    assert task_id > 0

    plan = {"test_type": "load", "target_interfaces": []}
    db.save_plan(task_id, plan)
    loaded = db.get_plan(task_id)
    assert loaded == plan

    task = db.get_task(task_id)
    assert task is not None
    assert task["requirement"] == "压测登录接口"

    db.update_task_status(task_id, "running")
    assert db.get_task(task_id)["status"] == "running"
    db.close()


def test_db_metric_points_roundtrip(tmp_path: str) -> None:
    db = Database(str(tmp_path / "test.db"))
    task_id = db.create_task("metric test")
    points = [
        {
            "task_id": task_id,
            "interface": "/login",
            "ts": 1.0,
            "users": 10.0,
            "rps": 5.0,
            "p50_ms": 50.0,
            "p95_ms": 100.0,
            "p99_ms": 150.0,
            "error_rate": 0.0,
            "error_count": 0,
            "cpu_percent": 20.0,
            "mem_percent": 30.0,
        }
    ]
    db.insert_metric_points(points)
    rows = db.get_metric_points(task_id)
    assert len(rows) == 1
    assert rows[0]["rps"] == 5.0
    db.close()
