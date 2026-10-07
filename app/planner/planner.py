"""Planner：自然语言需求 → 结构化 TestPlan。

流程：LLM 生成 JSON → Pydantic 校验 → 失败则带错误信息重试（最多 N 次）→ 仍失败抛明确错误。
"""

from __future__ import annotations

import json
import re

from pydantic import ValidationError

from app.llm.client import LLMClient, LLMMessage
from app.llm.prompts import PLANNER_RETRY_TEMPLATE, PLANNER_SYSTEM
from app.planner.models import TestPlan


class PlannerError(Exception):
    """方案生成失败（重试耗尽后仍无法通过校验）。"""


def _extract_json(text: str) -> str:
    """从 LLM 输出中提取 JSON（容忍 markdown 代码块包裹）。"""
    text = text.strip()
    # 去掉 ```json ... ``` 包裹
    m = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if m:
        text = m.group(1).strip()
    # 尝试定位第一个 { 到最后一个 }
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start : end + 1]
    return text


class Planner:
    def __init__(self, client: LLMClient, *, max_retries: int = 3) -> None:
        self._client = client
        self._max_retries = max_retries

    def generate(self, requirement: str) -> TestPlan:
        """把需求转成 TestPlan，校验失败自动重试。"""
        last_output = ""
        last_errors = ""

        for attempt in range(self._max_retries + 1):
            if attempt == 0:
                messages = [
                    LLMMessage(role="user", content=requirement),
                ]
            else:
                messages = [
                    LLMMessage(
                        role="user",
                        content=PLANNER_RETRY_TEMPLATE.format(
                            errors=last_errors,
                            requirement=requirement,
                            last_output=last_output,
                        ),
                    ),
                ]

            result = self._client.complete(messages, system=PLANNER_SYSTEM)
            last_output = result.text

            try:
                json_text = _extract_json(last_output)
                data = json.loads(json_text)
                return TestPlan.model_validate(data)
            except (json.JSONDecodeError, ValidationError, ValueError) as exc:
                last_errors = str(exc)
                # 继续重试

        raise PlannerError(
            f"方案生成失败：经过 {self._max_retries} 次重试仍无法通过校验。最后错误：{last_errors}"
        )
