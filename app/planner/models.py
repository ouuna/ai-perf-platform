"""领域模型：TestPlan 等 Pydantic schema。

所有 LLM 输出都通过这些 schema 校验，保证结构化、可验证。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, model_validator


class TestType(StrEnum):
    BASELINE = "baseline"
    LOAD = "load"
    STRESS = "stress"
    SOAK = "soak"
    SPIKE = "spike"


class TargetInterface(BaseModel):
    name: str = Field(description="接口唯一名称")
    method: str = Field(description="HTTP 方法")
    path: str = Field(description="请求路径")
    params: dict[str, str] = Field(default_factory=dict, description="可选参数")
    weight: int = Field(default=1, ge=1, description="负载权重，正整数")
    depends_on: str | None = Field(default=None, description="依赖的 token 接口 name")
    assert_status: int = Field(default=200, ge=100, le=599, description="断言状态码")


class LoadModel(BaseModel):
    start_users: int = Field(default=1, ge=1, description="起始用户数")
    step_users: int = Field(default=10, ge=1, description="每阶梯步长")
    step_duration_seconds: int = Field(default=60, ge=1, description="每阶梯时长")
    max_users: int = Field(default=100, ge=1, description="最大用户数")
    spawn_rate: float = Field(default=5, gt=0, description="每秒启动用户数")
    think_time_seconds: float = Field(default=1.0, ge=0, description="思考时间")


class SLA(BaseModel):
    p95_ms: float | None = Field(default=None, gt=0, description="P95 响应时间阈值 (ms)")
    p99_ms: float | None = Field(default=None, gt=0, description="P99 响应时间阈值 (ms)")
    max_error_rate: float | None = Field(default=None, ge=0, le=1, description="最大错误率")
    min_rps: float | None = Field(default=None, ge=0, description="最低吞吐 (RPS)")


class TestPlan(BaseModel):
    test_type: TestType = Field(description="测试类型")
    target_interfaces: list[TargetInterface] = Field(min_length=1, description="目标接口列表")
    load_model: LoadModel = Field(description="压测模型")
    sla: SLA = Field(default_factory=SLA, description="SLA 阈值")
    preconditions: list[str] = Field(default_factory=list, description="前置条件")
    notes: str = Field(default="", description="补充说明")

    @model_validator(mode="after")
    def _validate_dependencies(self) -> TestPlan:
        """校验 depends_on 引用的接口存在，且无循环依赖。"""
        names = {i.name for i in self.target_interfaces}
        for iface in self.target_interfaces:
            if iface.depends_on is not None and iface.depends_on not in names:
                raise ValueError(f"接口 '{iface.name}' 的 depends_on '{iface.depends_on}' 不存在")
            if iface.depends_on == iface.name:
                raise ValueError(f"接口 '{iface.name}' 不能依赖自身")
        return self
