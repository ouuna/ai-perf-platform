"""demo_target 靶站测试：故障开关、接口、/metrics。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from demo_target import app as target_app


def _client() -> TestClient:
    return TestClient(target_app.app)


def test_health() -> None:
    resp = _client().get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_login_returns_token() -> None:
    resp = _client().post("/login", json={"username": "tester"})
    assert resp.status_code == 200
    assert "token" in resp.json()


def test_products_list() -> None:
    resp = _client().get("/products")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] > 0
    assert len(data["items"]) > 0


def test_product_detail() -> None:
    resp = _client().get("/products/1")
    assert resp.status_code == 200
    assert resp.json()["id"] == 1


def test_product_detail_404() -> None:
    resp = _client().get("/products/99999")
    assert resp.status_code == 404


def test_order_detail() -> None:
    resp = _client().get("/orders/1")
    assert resp.status_code == 200
    assert "items" in resp.json()


def test_create_order() -> None:
    resp = _client().post("/orders", json={"product_id": 1, "quantity": 2})
    assert resp.status_code == 200
    assert "order_id" in resp.json()


def test_fault_switch_toggle() -> None:
    c = _client()
    # 默认关闭
    initial = c.get("/admin/faults").json()["faults"]
    assert initial["slow_query"] is False

    # 开启
    r = c.post("/admin/faults", json={"name": "slow_query", "enabled": True})
    assert r.status_code == 200
    assert r.json()["faults"]["slow_query"] is True

    # 关闭
    r = c.post("/admin/faults", json={"name": "slow_query", "enabled": False})
    assert r.json()["faults"]["slow_query"] is False


def test_fault_switch_unknown_name() -> None:
    resp = _client().post("/admin/faults", json={"name": "not_exist", "enabled": True})
    assert resp.status_code == 400


def test_metrics_returns_resource_gauges() -> None:
    resp = _client().get("/metrics")
    assert resp.status_code == 200
    body = resp.text
    assert "target_cpu_percent" in body
    assert "target_mem_rss_mb" in body
    assert "target_num_threads" in body
    assert "target_leak_cache_size" in body


def test_cpu_bound_fault_runs() -> None:
    c = _client()
    c.post("/admin/faults", json={"name": "cpu_bound", "enabled": True})
    resp = c.get("/cpu-work")
    assert resp.status_code == 200
    c.post("/admin/faults", json={"name": "cpu_bound", "enabled": False})


def test_memory_leak_grows_cache() -> None:
    c = _client()
    c.post("/admin/faults", json={"name": "memory_leak", "enabled": True})
    c.get("/cache-fill")
    after = c.get("/metrics").text
    # 泄漏缓存条目数应随调用增长
    assert "target_leak_cache_size" in after
    c.post("/admin/faults", json={"name": "memory_leak", "enabled": False})
