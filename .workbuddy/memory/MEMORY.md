# 项目长期笔记

## 关键技术决策
- 前端：单页 HTML + 原生 JS（不用 Streamlit），图表内嵌 SVG。
- Python 基线：3.11（本机 3.13 也兼容）。
- 脚本生成：Jinja2 模板渲染 + ast 静态校验，不让 LLM 自由写代码。
- Analyzer：规则层（确定性）先算 → AI 层只做解读 → 数字一致性校验器兜底。
- 指标统一为时间序列 metric_points 表，按时间戳对齐用户数/吞吐/延迟/错误率/资源。

## 环境约束
- 本机无 Docker，BIOS 虚拟化关闭（机械革命 i5-12450H）。Docker 交给 CI 验证，本机双进程跑。
- venv 用 `C:/Users/mechrev6/.workbuddy/binaries/python/versions/3.13.12/python.exe` 创建。
- 运行命令：`.venv/Scripts/python.exe -m pytest tests/`；lint：`.venv/Scripts/python.exe -m ruff check .`
