"""CLI 入口（typer）。"""

from __future__ import annotations

import typer

app = typer.Typer(help="AI 智能性能测试平台 CLI", no_args_is_help=True)


@app.command()
def version() -> None:
    """显示版本。"""
    from app import __version__

    typer.echo(f"ai-perf-platform {__version__}")


@app.command()
def hello() -> None:
    """冒烟：验证 CLI 可运行。"""
    typer.echo("Hello from ai-perf-platform CLI")


if __name__ == "__main__":
    app()
