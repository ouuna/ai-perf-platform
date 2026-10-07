"""Reporter：生成 HTML 与 Markdown 性能测试报告。

报告结构：
测试目的与范围 → 环境与压测模型 → SLA 结论摘要 → 接口指标表 → 趋势图 →
拐点与容量结论 → 瓶颈分析（证据+假设+置信度）→ 优化建议 → 风险与局限 → 附录。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from jinja2 import Environment, StrictUndefined

from app.analyzer.ai import AIAnalysis
from app.analyzer.rules import AnalysisResult
from app.planner.models import TestPlan

# ---------------------------------------------------------------------------
# 趋势图（内嵌 SVG）
# ---------------------------------------------------------------------------


def _line_chart_svg(
    title: str,
    series: list[dict],
    width: int = 640,
    height: int = 240,
) -> str:
    """生成一个简单的内嵌 SVG 折线图。

    series: [{"name": str, "points": [(x, y), ...], "color": str}, ...]
    """
    if not series:
        return ""

    all_x = [p[0] for s in series for p in s["points"]]
    all_y = [p[1] for s in series for p in s["points"]]
    if not all_x or not all_y:
        return ""

    min_x, max_x = min(all_x), max(all_x)
    min_y, max_y = min(all_y), max(all_y)
    if max_x == min_x:
        max_x = min_x + 1
    if max_y == min_y:
        max_y = min_y + 1

    pad_l, pad_r, pad_t, pad_b = 50, 20, 20, 40
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b

    def sx(x: float) -> float:
        return pad_l + (x - min_x) / (max_x - min_x) * plot_w

    def sy(y: float) -> float:
        return pad_t + (max_y - y) / (max_y - min_y) * plot_h

    parts = [f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg">']
    parts.append(
        f'<text x="{width / 2}" y="16" text-anchor="middle" font-size="14" '
        f'font-weight="bold" fill="#333">{title}</text>'
    )
    # 坐标轴
    parts.append(
        f'<line x1="{pad_l}" y1="{pad_t}" x2="{pad_l}" y2="{height - pad_b}" stroke="#999"/>'
    )
    parts.append(
        f'<line x1="{pad_l}" y1="{height - pad_b}" x2="{width - pad_r}" '
        f'y2="{height - pad_b}" stroke="#999"/>'
    )
    # Y 轴刻度（min/max）
    parts.append(
        f'<text x="{pad_l - 8}" y="{sy(max_y)}" text-anchor="end" font-size="10" fill="#666">'
        f"{max_y:.1f}</text>"
    )
    parts.append(
        f'<text x="{pad_l - 8}" y="{sy(min_y)}" text-anchor="end" font-size="10" fill="#666">'
        f"{min_y:.1f}</text>"
    )
    # X 轴刻度
    parts.append(
        f'<text x="{sx(min_x)}" y="{height - pad_b + 16}" text-anchor="middle" '
        f'font-size="10" fill="#666">{min_x:.0f}</text>'
    )
    parts.append(
        f'<text x="{sx(max_x)}" y="{height - pad_b + 16}" text-anchor="middle" '
        f'font-size="10" fill="#666">{max_x:.0f}</text>'
    )

    # 数据线
    for s in series:
        pts = s["points"]
        if len(pts) < 2:
            continue
        color = s.get("color", "#378ADD")
        path = "M " + " L ".join(f"{sx(p[0]):.1f} {sy(p[1]):.1f}" for p in pts)
        parts.append(f'<path d="{path}" fill="none" stroke="{color}" stroke-width="2"/>')
        # 图例
        parts.append(
            f'<text x="{width - pad_r - 10}" y="{16 + 14 * series.index(s)}" '
            f'text-anchor="end" font-size="10" fill="{color}">{s["name"]}</text>'
        )

    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# 报告数据模型
# ---------------------------------------------------------------------------


@dataclass
class ReportContext:
    """报告渲染所需的所有数据。"""

    requirement: str
    plan: TestPlan
    result: AnalysisResult
    ai: AIAnalysis | None
    metric_summary: list[dict]  # 每接口一行：name/rps/p95/p99/error_rate/requests
    trend_svg: str
    host_monitor_note: str = ""


# ---------------------------------------------------------------------------
# 模板
# ---------------------------------------------------------------------------

_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>性能测试报告</title>
<style>
  body { font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
         max-width: 900px; margin: 0 auto; padding: 24px; color: #222; line-height: 1.6; }
  h1 { border-bottom: 2px solid #378ADD; padding-bottom: 8px; }
  h2 { color: #185FA5; margin-top: 28px; }
  table { border-collapse: collapse; width: 100%; margin: 12px 0; }
  th, td { border: 1px solid #ccc; padding: 8px 12px; text-align: left; font-size: 14px; }
  th { background: #E6F1FB; }
  .pass { color: #0F6E56; font-weight: bold; }
  .fail { color: #A32D2D; font-weight: bold; }
  .fact { background: #F1EFE8; padding: 6px 10px; margin: 4px 0; border-radius: 4px; font-size: 13px; }
  .hypothesis { border-left: 3px solid #BA7517; padding: 8px 12px; margin: 8px 0; background: #FAEEDA; }
  .conf-high { color: #A32D2D; }
  .conf-medium { color: #BA7517; }
  .conf-low { color: #888780; }
  .chart { margin: 16px 0; }
</style>
</head>
<body>
<h1>性能测试报告</h1>

<h2>1. 测试目的与范围</h2>
<p>{{ requirement }}</p>

<h2>2. 环境与压测模型</h2>
<ul>
  <li>测试类型：{{ plan.test_type.value }}</li>
  <li>压测模型：起始 {{ plan.load_model.start_users }} 用户，
      步长 {{ plan.load_model.step_users }}，每阶梯 {{ plan.load_model.step_duration_seconds }}s，
      最大 {{ plan.load_model.max_users }} 用户，spawn rate {{ plan.load_model.spawn_rate }}</li>
  <li>接口数：{{ plan.target_interfaces | length }}</li>
  {% if plan.preconditions %}
  <li>前置条件：{{ plan.preconditions | join('; ') }}</li>
  {% endif %}
</ul>

<h2>3. SLA 与结论摘要</h2>
<table>
  <tr><th>接口</th><th>SLA 判定</th></tr>
  {% for iface, ok in result.sla_pass.items() %}
  <tr><td>{{ iface }}</td>
      <td class="{{ 'pass' if ok else 'fail' }}">{{ '通过' if ok else '未通过' }}</td></tr>
  {% endfor %}
</table>

<h2>4. 接口指标表</h2>
<table>
  <tr><th>接口</th><th>RPS</th><th>P50 (ms)</th><th>P95 (ms)</th><th>P99 (ms)</th><th>错误率</th></tr>
  {% for m in metric_summary %}
  <tr>
    <td>{{ m.name }}</td>
    <td>{{ "%.2f"|format(m.rps) if m.rps is not none else 'N/A' }}</td>
    <td>{{ "%.1f"|format(m.p50) if m.p50 is not none else 'N/A' }}</td>
    <td>{{ "%.1f"|format(m.p95) if m.p95 is not none else 'N/A' }}</td>
    <td>{{ "%.1f"|format(m.p99) if m.p99 is not none else 'N/A' }}</td>
    <td>{{ "%.2f%%"|format(m.error_rate * 100) if m.error_rate is not none else 'N/A' }}</td>
  </tr>
  {% endfor %}
</table>

<h2>5. 趋势图</h2>
<div class="chart">{{ trend_svg }}</div>

<h2>6. 拐点与容量结论</h2>
{% if result.max_stable_users is not none %}
<p>最大稳定并发：<strong>{{ "%.0f"|format(result.max_stable_users) }}</strong> 用户；
   估算容量：<strong>{{ "%.1f"|format(result.estimated_capacity_rps) }}</strong> RPS。</p>
{% else %}
<p>采样数据不足，无法可靠判断拐点。</p>
{% endif %}

<h2>7. 瓶颈分析（证据 + 假设 + 置信度）</h2>
{% if ai %}
  {% for h in ai.hypotheses %}
  <div class="hypothesis">
    <strong>假设：</strong>{{ h.statement }}<br>
    <span class="conf-{{ h.confidence }}">置信度：{{ h.confidence }}</span>
    {% if h.evidence_refs %}
    <br><span>证据：{{ h.evidence_refs | join(', ') }}</span>
    {% endif %}
  </div>
  {% endfor %}
  {% if ai.investigation_suggestions %}
  <h3>排查建议</h3>
  <ul>{% for s in ai.investigation_suggestions %}<li>{{ s }}</li>{% endfor %}</ul>
  {% endif %}
  {% if ai.optimization_suggestions %}
  <h3>优化建议</h3>
  <ul>{% for s in ai.optimization_suggestions %}<li>{{ s }}</li>{% endfor %}</ul>
  {% endif %}
{% else %}
<p>（未启用 AI 解读，仅规则分析）</p>
{% endif %}

<h2>8. 事实清单（规则层）</h2>
{% for f in result.facts %}
<div class="fact"><strong>[{{ f.id }}]</strong> ({{ f.kind }}) {{ f.statement }}
  {% if f.values %} <span style="color:#666">{{ f.values }}</span>{% endif %}</div>
{% endfor %}

<h2>9. 风险与局限</h2>
<ul>
  <li>{{ host_monitor_note or '压测机资源未监控' }}</li>
  <li>数据量、网络环境与生产环境可能存在差异，结论需结合实际评估。</li>
</ul>

<h2>10. 附录</h2>
<p>完整原始数据与脚本见任务产出的 CSV 与 locustfile。</p>

</body>
</html>
"""

_MD_TEMPLATE = """# 性能测试报告

## 1. 测试目的与范围
{{ requirement }}

## 2. 环境与压测模型
- 测试类型：{{ plan.test_type.value }}
- 压测模型：起始 {{ plan.load_model.start_users }} 用户，步长 {{ plan.load_model.step_users }}，每阶梯 {{ plan.load_model.step_duration_seconds }}s，最大 {{ plan.load_model.max_users }} 用户，spawn rate {{ plan.load_model.spawn_rate }}

## 3. SLA 与结论摘要
| 接口 | SLA 判定 |
|------|---------|
{% for iface, ok in result.sla_pass.items() %}| {{ iface }} | {{ '通过' if ok else '未通过' }} |
{% endfor %}
## 4. 接口指标表
| 接口 | RPS | P50 (ms) | P95 (ms) | P99 (ms) | 错误率 |
|------|-----|----------|----------|----------|--------|
{% for m in metric_summary %}| {{ m.name }} | {{ "%.2f"|format(m.rps) if m.rps is not none else 'N/A' }} | {{ "%.1f"|format(m.p50) if m.p50 is not none else 'N/A' }} | {{ "%.1f"|format(m.p95) if m.p95 is not none else 'N/A' }} | {{ "%.1f"|format(m.p99) if m.p99 is not none else 'N/A' }} | {{ "%.2f%%"|format(m.error_rate * 100) if m.error_rate is not none else 'N/A' }} |
{% endfor %}
## 5. 拐点与容量结论
{% if result.max_stable_users is not none %}- 最大稳定并发：**{{ "%.0f"|format(result.max_stable_users) }}** 用户
- 估算容量：**{{ "%.1f"|format(result.estimated_capacity_rps) }}** RPS
{% else %}- 采样数据不足，无法可靠判断拐点。
{% endif %}
## 6. 瓶颈分析
{% if ai %}{% for h in ai.hypotheses %}- **假设**：{{ h.statement }}（置信度：{{ h.confidence }}{% if h.evidence_refs %}，证据：{{ h.evidence_refs | join(', ') }}{% endif %}）
{% endfor %}{% if ai.investigation_suggestions %}
### 排查建议
{% for s in ai.investigation_suggestions %}- {{ s }}
{% endfor %}{% endif %}{% if ai.optimization_suggestions %}
### 优化建议
{% for s in ai.optimization_suggestions %}- {{ s }}
{% endfor %}{% endif %}{% else %}- （未启用 AI 解读，仅规则分析）
{% endif %}
## 7. 事实清单
{% for f in result.facts %}
- **[{{ f.id }}]** ({{ f.kind }}) {{ f.statement }}{% if f.values %} {{ f.values }}{% endif %}
{% endfor %}
## 8. 风险与局限
- {{ host_monitor_note or '压测机资源未监控' }}
- 数据量、网络环境与生产环境可能存在差异。
"""


class Reporter:
    def __init__(self) -> None:
        self._env = Environment(undefined=StrictUndefined, trim_blocks=True, lstrip_blocks=True)

    def _markdown_env(self) -> Environment:
        # Markdown 需要保留块标签后的换行，否则列表项会挤在一起
        return Environment(undefined=StrictUndefined)

    def render_html(self, ctx: ReportContext) -> str:
        template = self._env.from_string(_HTML_TEMPLATE)
        return template.render(
            requirement=ctx.requirement,
            plan=ctx.plan,
            result=ctx.result,
            ai=ctx.ai,
            metric_summary=ctx.metric_summary,
            trend_svg=ctx.trend_svg,
            host_monitor_note=ctx.host_monitor_note,
        )

    def render_markdown(self, ctx: ReportContext) -> str:
        template = self._markdown_env().from_string(_MD_TEMPLATE)
        return template.render(
            requirement=ctx.requirement,
            plan=ctx.plan,
            result=ctx.result,
            ai=ctx.ai,
            metric_summary=ctx.metric_summary,
            trend_svg=ctx.trend_svg,
            host_monitor_note=ctx.host_monitor_note,
        )

    def save(self, ctx: ReportContext, output_dir: str | Path) -> dict[str, Path]:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        html_path = output_dir / "report.html"
        md_path = output_dir / "report.md"
        html_path.write_text(self.render_html(ctx), encoding="utf-8")
        md_path.write_text(self.render_markdown(ctx), encoding="utf-8")
        return {"html": html_path, "markdown": md_path}
