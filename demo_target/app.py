"""demo_target：电商风格靶站，用于验证瓶颈定位准确性。

内置 6 种可注入的性能故障，每种有"标准答案"，通过环境变量或管理接口切换：
1. slow_query      — 商品列表模拟慢查询（无索引全表扫描，延迟随数据量上升）
2. n_plus_1        — 订单详情接口 N+1 查询
3. lock_contention — 下单接口全局锁竞争，高并发下吞吐封顶
4. pool_exhaustion — 数据库连接池过小，高并发出现等待与超时
5. memory_leak     — 某接口缓存无限增长，稳定性测试中内存和延迟持续上升
6. cpu_bound       — 某接口 CPU 密集，CPU 先饱和

暴露 /metrics（Prometheus 文本格式），提供 /admin/faults 管理接口。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel

app = FastAPI(title="demo_target", description="AI 性能测试平台内置靶站")

# ---------------------------------------------------------------------------
# 故障开关与状态
# ---------------------------------------------------------------------------

# 故障名 -> 是否启用。环境变量 AP_FAULTS="slow_query,n_plus_1" 可初始启用。
_DEFAULT_FAULTS = dict.fromkeys(
    ("slow_query", "n_plus_1", "lock_contention", "pool_exhaustion", "memory_leak", "cpu_bound"),
    False,
)
_FAULTS = dict(_DEFAULT_FAULTS)
_FAULT_LOCK = threading.Lock()

for _f in os.getenv("AP_FAULTS", "").split(","):
    _f = _f.strip()
    if _f in _FAULTS:
        _FAULTS[_f] = True

# 全局锁（用于 lock_contention 故障）
_GLOBAL_LOCK = threading.Lock()

# 无限增长缓存（用于 memory_leak 故障）
_LEAK_CACHE: dict[str, str] = {}

# 模拟数据库（内存中的商品表，数据量可调）
_PRODUCTS: list[dict] = [
    {"id": i, "name": f"商品-{i}", "price": 100 + i, "category": f"分类-{i % 10}"}
    for i in range(1, 501)
]
_ORDERS: dict[int, list[dict]] = {}

# 模拟慢查询延迟（数据量越大越慢）
_SLOW_QUERY_DELAY_MS = float(os.getenv("AP_SLOW_QUERY_DELAY_MS", "100"))

# 连接池大小（用于 pool_exhaustion，越小越容易耗尽）
_POOL_SIZE = int(os.getenv("AP_POOL_SIZE", "2"))


def _is_on(name: str) -> bool:
    with _FAULT_LOCK:
        return _FAULTS.get(name, False)


# ---------------------------------------------------------------------------
# 故障实现辅助函数
# ---------------------------------------------------------------------------


def _simulate_slow_query() -> None:
    """无索引全表扫描：延迟随数据量线性上升。"""
    if _is_on("slow_query"):
        # 模拟 O(N) 扫描，延迟与商品数成正比
        time.sleep(len(_PRODUCTS) * _SLOW_QUERY_DELAY_MS / 1000 / 500)


def _simulate_cpu_bound() -> None:
    """CPU 密集：大量哈希计算。"""
    if _is_on("cpu_bound"):
        for _ in range(5000):
            hashlib.sha256(b"perf-test" * 100).hexdigest()


def _simulate_memory_leak(path: str) -> None:
    """缓存无限增长，稳定测试中内存持续上升。"""
    if _is_on("memory_leak"):
        _LEAK_CACHE[f"{path}:{time.time_ns()}"] = json.dumps(_PRODUCTS)


def _simulate_pool_exhaustion() -> None:
    """连接池耗尽：通过信号量模拟过小连接池。"""
    if _is_on("pool_exhaustion"):
        # 用信号量模拟只有 _POOL_SIZE 个连接
        _pool_semaphore.acquire(timeout=0.05)


_pool_semaphore = threading.BoundedSemaphore(_POOL_SIZE)


# ---------------------------------------------------------------------------
# 业务接口
# ---------------------------------------------------------------------------


@app.post("/login")
def login(credentials: dict) -> JSONResponse:
    """登录：返回 token（demo 简化，任意用户名密码都通过）。"""
    username = credentials.get("username", "user")
    token = hashlib.sha256(f"{username}:{time.time_ns()}".encode()).hexdigest()[:32]
    return JSONResponse({"token": token, "user": username})


@app.get("/products")
def list_products() -> JSONResponse:
    """商品列表。slow_query 故障注入点。"""
    _simulate_slow_query()
    return JSONResponse({"count": len(_PRODUCTS), "items": _PRODUCTS[:20]})


@app.get("/products/{product_id}")
def get_product(product_id: int) -> JSONResponse:
    """商品详情。"""
    item = next((p for p in _PRODUCTS if p["id"] == product_id), None)
    if item is None:
        raise HTTPException(status_code=404, detail="product not found")
    return JSONResponse(item)


@app.get("/orders/{order_id}")
def get_order_detail(order_id: int) -> JSONResponse:
    """订单详情。n_plus_1 故障注入点：对每个商品逐条查询。"""
    items = _ORDERS.get(order_id, [{"id": 1, "name": "默认商品", "price": 100}])
    detail = []
    for it in items:
        # N+1：逐条查询商品信息，而非批量
        product = next((p for p in _PRODUCTS if p["id"] == it["id"]), it)
        if _is_on("n_plus_1"):
            time.sleep(0.01)  # 放大 N+1 效应
        detail.append(product)
    return JSONResponse({"order_id": order_id, "items": detail})


@app.post("/orders")
def create_order(order: dict) -> JSONResponse:
    """下单。lock_contention 故障注入点。"""
    product_id = order.get("product_id", 1)
    quantity = order.get("quantity", 1)

    if _is_on("lock_contention"):
        # 全局锁竞争：所有下单串行化，吞吐封顶
        with _GLOBAL_LOCK:
            time.sleep(0.05)
            order_id = len(_ORDERS) + 1
            _ORDERS[order_id] = [{"id": product_id, "name": f"商品-{product_id}", "price": 100}]
            return JSONResponse({"order_id": order_id, "quantity": quantity})

    order_id = len(_ORDERS) + 1
    _ORDERS[order_id] = [{"id": product_id, "name": f"商品-{product_id}", "price": 100}]
    return JSONResponse({"order_id": order_id, "quantity": quantity})


@app.get("/cache-fill")
def cache_fill() -> JSONResponse:
    """缓存填充接口。memory_leak 故障注入点。"""
    _simulate_memory_leak("/cache-fill")
    return JSONResponse({"cached": len(_LEAK_CACHE)})


@app.get("/cpu-work")
def cpu_work() -> JSONResponse:
    """CPU 密集接口。cpu_bound 故障注入点。"""
    _simulate_cpu_bound()
    return JSONResponse({"done": True})


@app.get("/pool-query")
def pool_query() -> JSONResponse:
    """数据库查询接口。pool_exhaustion 故障注入点。"""
    _simulate_pool_exhaustion()
    time.sleep(0.02)
    return JSONResponse({"rows": len(_PRODUCTS)})


# ---------------------------------------------------------------------------
# 运维接口
# ---------------------------------------------------------------------------


class FaultSwitch(BaseModel):
    name: str
    enabled: bool


@app.post("/admin/faults")
def set_fault(switch: FaultSwitch) -> JSONResponse:
    """管理接口：切换某个故障开关。"""
    if switch.name not in _FAULTS:
        raise HTTPException(status_code=400, detail=f"未知故障: {switch.name}")
    with _FAULT_LOCK:
        _FAULTS[switch.name] = switch.enabled
    return JSONResponse({"faults": dict(_FAULTS)})


@app.get("/admin/faults")
def get_faults() -> JSONResponse:
    """查看当前故障开关状态。"""
    with _FAULT_LOCK:
        return JSONResponse({"faults": dict(_FAULTS), "pool_size": _POOL_SIZE})


@app.get("/health")
def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})


@app.get("/metrics", response_class=PlainTextResponse)
def metrics() -> str:
    """Prometheus 文本格式的资源指标。"""
    import psutil

    proc = psutil.Process()
    cpu = psutil.cpu_percent(interval=None)
    mem = psutil.virtual_memory()
    rss = proc.memory_info().rss / (1024 * 1024)

    lines = [
        "# HELP target_cpu_percent 进程 CPU 使用率",
        "# TYPE target_cpu_percent gauge",
        f"target_cpu_percent {cpu:.2f}",
        "# HELP target_mem_rss_mb 进程内存 RSS (MB)",
        "# TYPE target_mem_rss_mb gauge",
        f"target_mem_rss_mb {rss:.2f}",
        "# HELP target_mem_percent 系统内存使用率",
        "# TYPE target_mem_percent gauge",
        f"target_mem_percent {mem.percent:.2f}",
        "# HELP target_num_threads 进程线程数",
        "# TYPE target_num_threads gauge",
        f"target_num_threads {proc.num_threads()}",
        "# HELP target_leak_cache_size 泄漏缓存条目数",
        "# TYPE target_leak_cache_size gauge",
        f"target_leak_cache_size {len(_LEAK_CACHE)}",
    ]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8001)
