"""数字一致性校验器：确保 AI 结论中引用的每个数字都能在事实清单中找到出处。

防止 AI 编造数字：AI 输出中出现的每个数值，必须与事实清单中的某个数值
"足够接近"（相对误差 < tolerance）。找不到出处的数字会被拒绝。
"""

from __future__ import annotations

import re

from app.analyzer.rules import AnalysisResult

# 匹配数字：整数、小数、百分数（含单位）
_NUMBER_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")


class NumberConsistencyError(Exception):
    """AI 引用了事实清单中不存在的数字。"""

    def __init__(self, missing_numbers: list[str]) -> None:
        self.missing_numbers = missing_numbers
        super().__init__(
            f"AI 结论引用了 {len(missing_numbers)} 个无法追溯的数字: {missing_numbers}"
        )


def _extract_numbers(text: str) -> list[float]:
    """提取文本中的所有数字。"""
    return [float(m) for m in _NUMBER_RE.findall(text)]


def _facts_number_pool(result: AnalysisResult) -> list[float]:
    """从事实清单中提取所有数字，作为"合法数字池"。"""
    pool: list[float] = []
    for fact in result.facts:
        for v in fact.values.values():
            pool.append(float(v))
        # 也提取 statement 中的数字（如置信度、百分比）
        pool.extend(_extract_numbers(fact.statement))
    return pool


def validate_numbers(text: str, result: AnalysisResult, tolerance: float = 0.05) -> None:
    """校验 AI 文本中的每个数字是否能在事实清单中找到出处。

    Args:
        text: AI 输出的文本。
        result: 规则层分析结果（事实清单）。
        tolerance: 相对误差容忍度（默认 5%）。

    Raises:
        NumberConsistencyError: 若存在无法追溯的数字。
    """
    pool = _facts_number_pool(result)
    missing: list[str] = []

    for num in _extract_numbers(text):
        # 跳过 0 和 1（可能是"1 个瓶颈"这类表述，不含实际测量意义）
        if num == 0:
            continue
        found = False
        for ref in pool:
            if ref == 0:
                continue
            if abs(num - ref) / abs(ref) <= tolerance:
                found = True
                break
        if not found:
            missing.append(str(num))

    if missing:
        raise NumberConsistencyError(missing)
