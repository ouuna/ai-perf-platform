"""Locust 脚本生成：Jinja2 模板渲染 + 静态校验 + dry-run。

不让 LLM 自由写代码，而是用结构化 TestPlan 渲染确定性的 locustfile，
再经过 ast 静态校验（import 白名单、危险调用拦截）和 dry-run 验证。
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

from jinja2 import Environment, StrictUndefined

from app.planner.models import TestPlan, TestType

_TEMPLATE = """# 由 ai-perf-platform 自动生成，勿手改
from locust import HttpUser, between, task
from locust import LoadTestShape


class GeneratedUser(HttpUser):
    wait_time = between({{ think_time_lo }}, {{ think_time_hi }})

    {% for iface in login_interfaces %}
    def login_{{ iface.name }}(self):
        {% if iface.params %}
        resp = self.client.post(
            "{{ iface.path }}",
            json={{ iface.params | tojson }},
            catch_response=True,
        )
        {% else %}
        resp = self.client.post("{{ iface.path }}", catch_response=True)
        {% endif %}
        if resp.status_code != {{ iface.assert_status }}:
            resp.failure(f"login status {resp.status_code}")
        else:
            self.token = resp.json().get("token", "")

    {% endfor %}

    {% for iface in task_interfaces %}
    @task({{ iface.weight }})
    def task_{{ iface.name }}(self):
        headers = {}
        {% if iface.depends_on %}
        if not hasattr(self, "token"):
            self.login_{{ iface.depends_on }}()
        headers = {"Authorization": f"Bearer {self.token}"}
        {% endif %}
        {% if iface.params %}
        resp = self.client.request(
            "{{ iface.method }}",
            "{{ iface.path }}",
            headers=headers,
            json={{ iface.params | tojson }},
            name="{{ iface.name }}",
        )
        {% else %}
        resp = self.client.request(
            "{{ iface.method }}",
            "{{ iface.path }}",
            headers=headers,
            name="{{ iface.name }}",
        )
        {% endif %}
        {% if iface.assert_status %}
        if resp.status_code != {{ iface.assert_status }}:
            resp.failure(f"{{ iface.name }} status {resp.status_code}")
        {% endif %}
    {% endfor %}


{% if shape %}
class {{ shape }}(LoadTestShape):
    def tick(self):
        run_time = self.get_run_time()
        {% for step in shape_steps %}
        if run_time < {{ step.end }}:
            users = {{ step.users }}
            spawn_rate = {{ step.spawn_rate }}
            return (users, spawn_rate)
        {% endfor %}
        return None
{% endif %}
"""


def _shape_for(plan: TestPlan) -> tuple[str, list[dict]] | None:
    """根据测试类型生成 LoadTestShape 阶梯配置。"""
    lm = plan.load_model
    if plan.test_type in {TestType.SPIKE}:
        return None, None  # spike 用不同的 shape，这里简化处理

    steps: list[dict] = []
    users = lm.start_users
    t = 0.0
    # 阶梯加压
    while users < lm.max_users:
        t += lm.step_duration_seconds
        steps.append({"end": t, "users": users, "spawn_rate": lm.spawn_rate})
        users = min(users + lm.step_users, lm.max_users)
    # 最后一级保持 max_users
    t += lm.step_duration_seconds
    steps.append({"end": t, "users": lm.max_users, "spawn_rate": lm.spawn_rate})

    return "StepLoadShape", steps


def _render_template(plan: TestPlan) -> str:
    """渲染 locustfile 源码。"""
    env = Environment(undefined=StrictUndefined, trim_blocks=True, lstrip_blocks=True)
    template = env.from_string(_TEMPLATE)

    # 登录接口：无依赖，且 path 或 name 含 "login"
    login_interfaces = [
        i
        for i in plan.target_interfaces
        if i.depends_on is None and ("login" in i.path.lower() or "login" in i.name.lower())
    ]
    task_interfaces = [
        i for i in plan.target_interfaces if i not in login_interfaces
    ]

    shape_name, shape_steps = _shape_for(plan)

    ctx = {
        "think_time_lo": plan.load_model.think_time_seconds,
        "think_time_hi": plan.load_model.think_time_seconds + 0.5,
        "login_interfaces": login_interfaces,
        "task_interfaces": task_interfaces,
        "shape": shape_name,
        "shape_steps": shape_steps or [],
    }

    return template.render(**ctx)


# ---- 静态校验 ----

_FORBIDDEN_CALLS = {"os.system", "subprocess", "eval", "exec", "open", "pickle"}
_ALLOWED_IMPORTS = {"locust", "random", "json", "time", "csv"}


class ScriptGenError(Exception):
    """脚本生成或校验失败。"""


def _static_check(source: str) -> list[str]:
    """静态校验：语法、import 白名单、危险调用。返回问题列表。"""
    problems: list[str] = []

    # 1. 语法校验
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [f"语法错误: {exc}"]

    # 2. import 白名单
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root not in _ALLOWED_IMPORTS:
                    problems.append(f"禁止 import: {alias.name}")
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.split(".")[0] not in _ALLOWED_IMPORTS:
                problems.append(f"禁止 import: {node.module}")

    # 3. 危险调用
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = ""
            if isinstance(node.func, ast.Name):
                name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                name = node.func.attr
            if name in _FORBIDDEN_CALLS:
                problems.append(f"禁止调用: {name}")

    return problems


def generate_locustfile(plan: TestPlan) -> str:
    """生成 locustfile 源码并做静态校验。"""
    source = _render_template(plan)
    problems = _static_check(source)
    if problems:
        raise ScriptGenError("脚本静态校验失败: " + "; ".join(problems))
    return source


def write_locustfile(plan: TestPlan, directory: str | Path) -> Path:
    """生成并写入 locustfile.py，返回文件路径。"""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    source = generate_locustfile(plan)
    path = directory / "locustfile.py"
    path.write_text(source, encoding="utf-8")
    return path


def dry_run(plan: TestPlan, target_url: str, directory: str | Path) -> dict:
    """dry-run：极低并发、短时运行，验证脚本可执行。返回结果摘要。

    为 dry-run 生成独立的简化脚本（去掉 LoadTestShape），
    避免 shape 的运行时长覆盖 -t 导致测试无法按预期短时结束。
    """
    import sys

    # 用简化方案（无 shape）生成脚本，保证 -t 3s 生效
    simple = plan.model_copy(update={"load_model": plan.load_model.model_copy(update={
        "start_users": 1, "max_users": 1, "step_users": 1, "step_duration_seconds": 1,
    })})
    path = write_locustfile(simple, directory)

    cmd = [sys.executable, "-m", "locust", "-f", str(path), "--headless", "--host",
           target_url, "-u", "1", "-r", "1", "-t", "3s", "--only-summary"]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except FileNotFoundError as exc:
        raise ScriptGenError("未找到 locust，请先安装 locust 依赖") from exc
    except subprocess.TimeoutExpired as exc:
        raise ScriptGenError("dry-run 超时") from exc

    return {
        "returncode": proc.returncode,
        "stdout": proc.stdout[-2000:],
        "stderr": proc.stderr[-2000:],
    }
