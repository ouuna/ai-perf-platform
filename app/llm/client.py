"""LLM 客户端抽象层。

统一接口 LLMClient，支持三种实现：
- AnthropicClient：Anthropic Messages API
- OpenAIClient：OpenAI 兼容 Chat Completions API
- MockLLM：无网络、无 Key 的确定性 mock，用于 demo 与测试

所有实现输出纯文本（通常是 JSON），由上层 planner/analyzer 做 schema 校验。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class LLMMessage:
    role: str  # "system" | "user" | "assistant"
    content: str


@dataclass
class LLMResult:
    text: str
    model: str = ""
    raw: object = None


class LLMError(Exception):
    """LLM 调用失败（网络/认证/超时等）。"""


class LLMClient(ABC):
    """所有 LLM 实现的统一抽象。"""

    @abstractmethod
    def complete(
        self,
        messages: list[LLMMessage],
        *,
        system: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> LLMResult:
        """发送多轮对话，返回文本结果。"""


class _OpenAICompatClient(LLMClient):
    """OpenAI 兼容接口（同时作为 Anthropic 之外的第二实现）。"""

    def __init__(self, api_key: str, model: str, base_url: str = "") -> None:
        if not api_key:
            raise LLMError("OpenAI 兼容接口需要 API Key")
        self._model = model or "gpt-4o-mini"
        self._base_url = base_url
        # 延迟 import，避免无 key 环境加载 openai 依赖
        from openai import OpenAI

        kwargs = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = OpenAI(**kwargs)

    def complete(
        self,
        messages: list[LLMMessage],
        *,
        system: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> LLMResult:
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.extend({"role": m.role, "content": m.content} for m in messages)
        try:
            resp = self._client.chat.completions.create(
                model=self._model,
                messages=msgs,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except Exception as exc:  # noqa: BLE001 - 统一包装为 LLMError
            raise LLMError(f"OpenAI 调用失败: {exc}") from exc
        text = resp.choices[0].message.content or ""
        return LLMResult(text=text, model=self._model, raw=resp)


class AnthropicClient(LLMClient):
    """Anthropic Messages API 实现。"""

    def __init__(self, api_key: str, model: str) -> None:
        if not api_key:
            raise LLMError("Anthropic 需要 API Key")
        self._model = model or "claude-sonnet-4-5"
        from anthropic import Anthropic

        self._client = Anthropic(api_key=api_key)

    def complete(
        self,
        messages: list[LLMMessage],
        *,
        system: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> LLMResult:
        # Anthropic 要求 system 单独传，messages 中不能含 system 角色
        conv = [m for m in messages if m.role != "system"]
        try:
            resp = self._client.messages.create(
                model=self._model,
                system=system,
                messages=[{"role": m.role, "content": m.content} for m in conv],
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except Exception as exc:  # noqa: BLE001
            raise LLMError(f"Anthropic 调用失败: {exc}") from exc
        text = "".join(
            block.text for block in resp.content if getattr(block, "type", "") == "text"
        )
        return LLMResult(text=text, model=self._model, raw=resp)


@dataclass
class MockLLM(LLMClient):
    """确定性 mock：无网络无 Key，返回可配置的响应序列。

    - `responses`: 按顺序返回的文本；耗尽后返回 `default_response`。
    - 用于测试与 demo，保证全链路可离线复现。
    """

    responses: list[str] = field(default_factory=list)
    default_response: str = '{"message": "mock"}'

    def complete(
        self,
        messages: list[LLMMessage],
        *,
        system: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> LLMResult:
        if self.responses:
            text = self.responses.pop(0)
        else:
            text = self.default_response
        return LLMResult(text=text, model="mock", raw=None)


def create_llm_client(provider: str | None = None) -> LLMClient:
    """根据配置创建 LLM 客户端。provider 优先级：参数 > 环境变量 > mock。

    无 Key 时使用 mock，且 mock 默认返回一个合法的示例 TestPlan，
    保证 demo 和 API 在无 Key 情况下也能完整跑通全链路。
    """
    from app.config import get_settings

    settings = get_settings()
    provider = (provider or settings.llm_provider).strip().lower()

    if provider == "mock":
        return MockLLM(default_response=_DEMO_PLAN_JSON)
    if provider == "anthropic":
        return AnthropicClient(settings.anthropic_api_key, settings.llm_model)
    if provider in {"openai", "openai-compatible"}:
        return _OpenAICompatClient(
            settings.openai_api_key, settings.llm_model, settings.openai_base_url
        )
    raise LLMError(f"未知的 LLM provider: {provider}")


# 内置示例方案：MockLLM 在无 Key 场景下的默认返回，保证 demo 可跑通
_DEMO_PLAN_JSON = (
    '{"test_type":"load","target_interfaces":['
    '{"name":"login","method":"POST","path":"/login",'
    '"params":{"username":"demo"},"weight":1,"depends_on":null,"assert_status":200},'
    '{"name":"list_products","method":"GET","path":"/products",'
    '"params":{},"weight":3,"depends_on":"login","assert_status":200},'
    '{"name":"product_detail","method":"GET","path":"/products/1",'
    '"params":{},"weight":2,"depends_on":"login","assert_status":200}],'
    '"load_model":{"start_users":1,"step_users":2,"step_duration_seconds":3,'
    '"max_users":5,"spawn_rate":1,"think_time_seconds":0.1},'
    '"sla":{"p95_ms":1000,"p99_ms":2000,"max_error_rate":0.05,"min_rps":null},'
    '"preconditions":[],"notes":"mock 示例方案"}'
)
