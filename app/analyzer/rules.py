"""Analyzer 规则层：确定性计算，输出"事实清单"。

规则先于 AI：先做确定性的拐点检测、SLA 判定、异常模式识别、资源相关性分析，
产出一份带编号的"事实清单"，AI 层只能基于这份清单做解读。

事实清单（Fact）有唯一编号（F1, F2, ...），AI 引用时必须以编号 + 数值形式，
由数字一致性校验器核对。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.db import Database
from app.planner.models import SLA, TestPlan


@dataclass
class Fact:
    """一条事实：带编号、类型、描述、关键数值。"""

    id: str
    kind: str  # knee | sla | anomaly | resource | capacity
    statement: str
    values: dict[str, float] = field(default_factory=dict)


@dataclass
class AnalysisResult:
    """规则层分析结果。"""

    facts: list[Fact] = field(default_factory=list)
    max_stable_users: float | None = None
    estimated_capacity_rps: float | None = None
    sla_pass: dict[str, bool] = field(default_factory=dict)

    def fact_ids(self) -> list[str]:
        return [f.id for f in self.facts]


def _series(rows, key: str) -> list[tuple[float, float]]:
    """从 metric_points 行中提取 (ts, value) 序列，跳过 None。"""
    out: list[tuple[float, float]] = []
    for r in rows:
        v = r[key]
        if v is not None:
            out.append((float(r["ts"]), float(v)))
    return out


# ---------------------------------------------------------------------------
# 拐点检测
# ---------------------------------------------------------------------------


def detect_knee(rows) -> tuple[float | None, float | None]:
    """拐点检测：吞吐不再随并发增长、而延迟显著上升的位置。

    返回 (最大稳定并发用户数, 该点的吞吐)。

    算法：
    1. 按用户数升序聚合：每个用户数对应的平均吞吐、平均 P95。
    2. 吞吐曲线：找到吞吐增长明显放缓（增速 < 前一阶段的 30%）且延迟上升的位置。
    """
    # 用 Aggregated 行（interface is None）
    agg = [r for r in rows if r["interface"] is None]
    if len(agg) < 3:
        return None, None

    # 按时间排序
    agg_sorted = sorted(agg, key=lambda r: float(r["ts"]))

    # 分段：把时间序列按用户数单调递增切分（阶梯加压）
    # 简化：直接用时间序列，找"吞吐增量下降 + P95 上升"的拐点
    users = [float(r["users"] or 0) for r in agg_sorted]
    rps = [float(r["rps"] or 0) for r in agg_sorted]
    p95 = [float(r["p95_ms"] or 0) for r in agg_sorted]

    # 找吞吐峰值对应的用户数（吞吐不再显著增长的点）
    peak_rps = max(rps)
    peak_idx = rps.index(peak_rps)

    # 拐点 = 吞吐达到峰值的 90% 且之后延迟显著上升的点
    threshold_rps = peak_rps * 0.9
    knee_users: float | None = None
    knee_rps: float | None = None

    for i in range(len(agg_sorted)):
        if rps[i] >= threshold_rps and i > 0:
            # 检查延迟是否开始上升（P95 比前一阶段高 20% 以上）
            if p95[i] > p95[i - 1] * 1.2 or i == peak_idx:
                knee_users = users[i]
                knee_rps = rps[i]
                break

    if knee_users is None:
        knee_users = users[peak_idx]
        knee_rps = rps[peak_idx]

    return knee_users, knee_rps


# ---------------------------------------------------------------------------
# SLA 判定
# ---------------------------------------------------------------------------


def evaluate_sla(rows, sla: SLA) -> dict[str, bool]:
    """逐接口判定 SLA 是否达标。返回 {interface: pass}。"""
    result: dict[str, bool] = {}
    # 按接口分组
    by_iface: dict[str, list] = {}
    for r in rows:
        key = r["interface"] or "Aggregated"
        by_iface.setdefault(key, []).append(r)

    for iface, iface_rows in by_iface.items():
        # 用该接口所有采样的最差值（或均值）判断
        p95_vals = [float(r["p95_ms"]) for r in iface_rows if r["p95_ms"] is not None]
        err_vals = [float(r["error_rate"]) for r in iface_rows if r["error_rate"] is not None]

        ok = True
        if sla.p95_ms is not None and p95_vals:
            if max(p95_vals) > sla.p95_ms:
                ok = False
        if sla.max_error_rate is not None and err_vals:
            if max(err_vals) > sla.max_error_rate:
                ok = False

        result[iface] = ok

    return result


# ---------------------------------------------------------------------------
# 异常模式识别
# ---------------------------------------------------------------------------


def detect_anomalies(rows) -> list[Fact]:
    """识别异常模式：延迟持续上涨、错误率突增、长尾、吞吐锯齿。"""
    facts: list[Fact] = []
    agg = sorted([r for r in rows if r["interface"] is None], key=lambda r: float(r["ts"]))
    if len(agg) < 5:
        return facts

    p95 = [float(r["p95_ms"] or 0) for r in agg]
    err = [float(r["error_rate"] or 0) for r in agg]
    rps = [float(r["rps"] or 0) for r in agg]

    # 1. 延迟持续上涨（疑似泄漏/积压）：后 1/3 的 P95 均值 > 前 1/3 的 1.5 倍
    third = len(p95) // 3
    if third >= 2:
        head_p95 = sum(p95[:third]) / third
        tail_p95 = sum(p95[-third:]) / third
        if tail_p95 > head_p95 * 1.5 and tail_p95 > head_p95 + 20:
            facts.append(
                Fact(
                    id="A1",
                    kind="anomaly",
                    statement="延迟随时间持续上涨，疑似资源泄漏或请求积压",
                    values={"head_p95_ms": head_p95, "tail_p95_ms": tail_p95},
                )
            )

    # 2. 错误率突增：后半段错误率均值 > 前半段 3 倍且 > 1%
    half = len(err) // 2
    if half >= 2:
        head_err = sum(err[:half]) / half
        tail_err = sum(err[half:]) / half
        if tail_err > head_err * 3 and tail_err > 0.01:
            facts.append(
                Fact(
                    id="A2",
                    kind="anomaly",
                    statement="错误率在后半段突增，可能存在容量不足或依赖故障",
                    values={"head_error_rate": head_err, "tail_error_rate": tail_err},
                )
            )

    # 3. 长尾：P99/P50 比值过大（用均值近似，因 metric 存 p50/p95/p99）
    # 这里用 p95/p50 比值（如果采集了 p50）
    # metric_points 有 p50_ms 字段
    p50 = [float(r["p50_ms"] or 0) for r in agg if r["p50_ms"] is not None]
    p95_all = [float(r["p95_ms"] or 0) for r in agg if r["p95_ms"] is not None]
    if p50 and p95_all:
        avg_p50 = sum(p50) / len(p50)
        avg_p95 = sum(p95_all) / len(p95_all)
        if avg_p50 > 0 and avg_p95 / avg_p50 > 3:
            facts.append(
                Fact(
                    id="A3",
                    kind="anomaly",
                    statement="响应时间长尾明显（P95/P50 比值过大），存在慢请求或排队",
                    values={"avg_p50_ms": avg_p50, "avg_p95_ms": avg_p95},
                )
            )

    # 4. 吞吐锯齿：RPS 波动系数 > 0.5（标准差/均值）
    if len(rps) >= 5:
        mean_rps = sum(rps) / len(rps)
        if mean_rps > 0:
            std = (sum((x - mean_rps) ** 2 for x in rps) / len(rps)) ** 0.5
            if std / mean_rps > 0.5:
                facts.append(
                    Fact(
                        id="A4",
                        kind="anomaly",
                        statement="吞吐出现明显锯齿波动，可能存在锁竞争或间歇性阻塞",
                        values={"mean_rps": mean_rps, "std_rps": std},
                    )
                )

    return facts


# ---------------------------------------------------------------------------
# 资源相关性
# ---------------------------------------------------------------------------


def detect_resource_correlation(rows) -> list[Fact]:
    """服务端/压测机资源相关性：CPU 饱和、内存持续增长、连接池耗尽信号。"""
    facts: list[Fact] = []
    agg = sorted([r for r in rows if r["interface"] is None], key=lambda r: float(r["ts"]))
    if len(agg) < 5:
        return facts

    cpu = [float(r["cpu_percent"]) for r in agg if r["cpu_percent"] is not None]
    mem = [float(r["mem_percent"]) for r in agg if r["mem_percent"] is not None]

    # CPU 饱和：平均 CPU > 85%
    if cpu:
        avg_cpu = sum(cpu) / len(cpu)
        if avg_cpu > 85:
            facts.append(
                Fact(
                    id="R1",
                    kind="resource",
                    statement="CPU 使用率接近饱和，CPU 可能成为瓶颈",
                    values={"avg_cpu_percent": avg_cpu},
                )
            )

    # 内存持续增长：后半段均值 > 前半段 1.3 倍
    if len(mem) >= 4:
        half = len(mem) // 2
        head_mem = sum(mem[:half]) / half
        tail_mem = sum(mem[half:]) / half
        if tail_mem > head_mem * 1.3 and tail_mem > head_mem + 5:
            facts.append(
                Fact(
                    id="R2",
                    kind="resource",
                    statement="内存持续增长，可能存在内存泄漏或缓存无限累积",
                    values={"head_mem": head_mem, "tail_mem": tail_mem},
                )
            )

    return facts


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def run_rule_analysis(task_id: int, plan: TestPlan, db: Database) -> AnalysisResult:
    """执行完整规则分析，返回事实清单 + 容量结论。"""
    rows = db.get_metric_points(task_id)
    result = AnalysisResult()

    # 拐点
    knee_users, knee_rps = detect_knee(rows)
    if knee_users is not None:
        result.max_stable_users = knee_users
        result.estimated_capacity_rps = knee_rps
        result.facts.append(
            Fact(
                id="F1",
                kind="knee",
                statement="检测到吞吐拐点：并发超过该点后吞吐不再增长、延迟上升",
                values={"max_stable_users": knee_users, "capacity_rps": knee_rps or 0},
            )
        )

    # SLA
    sla_result = evaluate_sla(rows, plan.sla)
    result.sla_pass = sla_result
    for iface, ok in sla_result.items():
        result.facts.append(
            Fact(
                id=f"S-{iface}",
                kind="sla",
                statement=f"接口 '{iface}' SLA 判定：{'通过' if ok else '未通过'}",
                values={"pass": float(ok)},
            )
        )

    # 异常模式
    result.facts.extend(detect_anomalies(rows))

    # 资源相关性
    result.facts.extend(detect_resource_correlation(rows))

    # 重新编号（保证 F1..Fn 连续）
    for i, f in enumerate(result.facts, start=1):
        f.id = f"F{i}"

    return result
