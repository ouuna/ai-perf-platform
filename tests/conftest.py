"""pytest 全局配置。"""

from __future__ import annotations

# 领域模型类名以 Test 开头，避免被 pytest 误收集为测试类
from app.planner.models import TestPlan, TestType

TestPlan.__test__ = False  # type: ignore[attr-defined]
TestType.__test__ = False  # type: ignore[attr-defined]
