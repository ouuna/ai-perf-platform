"""Runner：以子进程运行 locust，实时读进度、超时、中止、SLA 熔断。"""

from __future__ import annotations

import csv
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.planner.models import TestPlan
from app.scriptgen.generator import write_locustfile


class RunnerError(Exception):
    """压测执行失败。"""


class SLAExceededError(Exception):
    """SLA 熔断：错误率超过阈值，提前终止。"""


@dataclass
class RunResult:
    """一次压测的完整结果。"""

    returncode: int
    stdout: str
    stderr: str
    csv_dir: Path
    stopped_by_sla: bool = False
    sla_reason: str = ""
    duration_seconds: float = 0.0
    error_rate_at_break: float = 0.0


@dataclass
class Runner:
    plan: TestPlan
    target_url: str
    csv_dir: str | Path
    sla_error_rate_break: float = 0.30
    timeout_seconds: int = 1200

    _proc: subprocess.Popen | None = field(default=None, init=False, repr=False)
    _stop_event: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _stats_file: Path | None = field(default=None, init=False, repr=False)

    def _build_cmd(self, locustfile: Path) -> list[str]:
        lm = self.plan.load_model
        return [
            sys.executable,
            "-m",
            "locust",
            "-f",
            str(locustfile),
            "--headless",
            "--host",
            self.target_url,
            "-u",
            str(lm.max_users),
            "-r",
            str(lm.spawn_rate),
            "--csv",
            str(Path(self.csv_dir) / "stats"),
            "--csv-full-history",
        ]

    def run(self) -> RunResult:
        """同步执行压测（内部管理子进程生命周期、SLA 熔断、超时）。"""
        csv_dir = Path(self.csv_dir)
        csv_dir.mkdir(parents=True, exist_ok=True)
        locustfile = write_locustfile(self.plan, csv_dir)

        cmd = self._build_cmd(locustfile)
        # 将 stdout/stderr 重定向到文件，避免 PIPE 缓冲区写满导致子进程死锁
        log_path = csv_dir / "locust.log"
        log_file = log_path.open("w", encoding="utf-8", errors="replace")

        start = time.time()
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except FileNotFoundError as exc:
            log_file.close()
            raise RunnerError("未找到 locust，请先安装 locust") from exc

        self._stats_file = Path(csv_dir) / "stats_stats_history.csv"

        # 轮询：检查结束、超时、SLA 熔断、手动中止
        sla_broken = False
        sla_reason = ""
        error_at_break = 0.0

        while self._proc.poll() is None:
            # 超时
            if time.time() - start > self.timeout_seconds:
                self.stop()
                raise RunnerError(f"压测超过超时上限 {self.timeout_seconds}s，已中止")

            # 手动中止
            if self._stop_event.is_set():
                self.stop()
                break

            # SLA 熔断
            err = self._current_error_rate()
            if err is not None and err > self.sla_error_rate_break:
                sla_broken = True
                sla_reason = f"错误率 {err:.2%} 超过熔断阈值 {self.sla_error_rate_break:.2%}"
                error_at_break = err
                self.stop()
                break

            time.sleep(1.0)

        self._proc.wait(timeout=10)
        log_file.close()

        log_content = log_path.read_text(encoding="utf-8", errors="replace")

        return RunResult(
            returncode=self._proc.returncode,
            stdout=log_content,
            stderr="",
            csv_dir=csv_dir,
            stopped_by_sla=sla_broken,
            sla_reason=sla_reason,
            duration_seconds=time.time() - start,
            error_rate_at_break=error_at_break,
        )

    def stop(self) -> None:
        """请求中止（供 UI/API 轮询调用）。"""
        self._stop_event.set()
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()

    def _current_error_rate(self) -> float | None:
        """从 stats history CSV 读取最近一秒的全局错误率。"""
        if self._stats_file is None or not self._stats_file.exists():
            return None
        try:
            with self._stats_file.open(newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                rows = list(reader)
            if not rows:
                return None
            last = rows[-1]
            # Aggregate 行
            agg_rows = [r for r in rows if r.get("Name") == "Aggregated"]
            if agg_rows:
                last = agg_rows[-1]
            requests = int(float(last.get("Request Count", 0)))
            failures = int(float(last.get("Failure Count", 0)))
            if requests == 0:
                return 0.0
            return failures / requests
        except (OSError, ValueError, KeyError):
            return None

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None
