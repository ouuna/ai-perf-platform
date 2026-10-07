"""Analyzer AI 层：基于规则层事实清单做解读。

严格约束：
1. AI 只能基于事实清单 + 原始统计做解读。
2. AI 结论中的每个数字必须能追溯到事实清单，由数字一致性校验器兜底。
3. 不确定时必须说"证据不足"，不得编造。
"""

from __future__ import annotations

import json
import re

from pydantic import BaseModel, Field, ValidationError

from app.analyzer.consistency import NumberConsistencyError, validate_numbers
from app.analyzer.rules import AnalysisResult
from app.llm.client import LLMClient, LLMMessage
from app.llm.prompts import ANALYZER_FACTS_TEMPLATE, ANALYZER_SYSTEM


class Hypothesis(BaseModel):
    statement: str = Field(description="瓶颈假设")
    confidence: str = Field(description="置信度 high/medium/low")
    evidence_refs: list[str] = Field(default_factory=list, description="证据引用（事实编号）")


class AIAnalysis(BaseModel):
    hypotheses: list[Hypothesis] = Field(description="瓶颈假设列表")
    investigation_suggestions: list[str] = Field(default_factory=list)
    optimization_suggestions: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class AnalyzerError(Exception):
    """AI 解读失败（重试耗尽）。"""


def _extract_json(text: str) -> str:
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if m:
        text = m.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start : end + 1]
    return text


def _facts_text(result: AnalysisResult) -> str:
    lines = []
    for f in result.facts:
        lines.append(f"[{f.id}] ({f.kind}) {f.statement} {f.values}")
    return "\n".join(lines)


def _stats_text(rows) -> str:
    """原始统计摘要（供 AI 参考，不含结论）。"""
    agg = [r for r in rows if r["interface"] is None]
    if not agg:
        return "无聚合统计数据"
    n = len(agg)
    return (
        f"聚合采样点数: {n}; "
        f"平均吞吐: {sum(float(r['rps'] or 0) for r in agg) / n:.2f} RPS; "
        f"平均 P95: {sum(float(r['p95_ms'] or 0) for r in agg) / n:.2f} ms; "
        f"平均错误率: {sum(float(r['error_rate'] or 0) for r in agg) / n:.4f}"
    )


class AIAnalyzer:
    def __init__(self, client: LLMClient, *, max_retries: int = 3) -> None:
        self._client = client
        self._max_retries = max_retries

    def analyze(self, result: AnalysisResult, rows) -> AIAnalysis:
        """基于事实清单 + 原始统计做解读，带数字一致性校验与重试。"""
        facts = _facts_text(result)
        stats = _stats_text(rows)

        last_output = ""
        last_errors = ""

        for attempt in range(self._max_retries + 1):
            prompt = ANALYZER_FACTS_TEMPLATE.format(facts=facts, stats=stats)
            if attempt > 0:
                retry_note = (
                    f"\n\n上次输出校验失败：{last_errors}\n"
                    f"请修正后重新输出。上次输出：\n{last_output}"
                )
                prompt += retry_note

            resp = self._client.complete(
                [LLMMessage(role="user", content=prompt)], system=ANALYZER_SYSTEM
            )
            last_output = resp.text

            try:
                data = json.loads(_extract_json(last_output))
                analysis = AIAnalysis.model_validate(data)

                # 数字一致性校验：AI 结论中每个数字必须能追溯
                full_text = " ".join(
                    [h.statement for h in analysis.hypotheses]
                    + analysis.investigation_suggestions
                    + analysis.optimization_suggestions
                )
                validate_numbers(full_text, result)

                # 校验证据引用：evidence_refs 必须是事实清单里的编号
                valid_ids = set(result.fact_ids())
                for h in analysis.hypotheses:
                    for ref in h.evidence_refs:
                        if ref not in valid_ids:
                            raise NumberConsistencyError([f"证据引用 {ref} 不存在"])

                return analysis

            except (json.JSONDecodeError, ValidationError, NumberConsistencyError) as exc:
                last_errors = str(exc)
                continue

        raise AnalyzerError(f"AI 解读失败：经过 {self._max_retries} 次重试仍无法通过校验")
