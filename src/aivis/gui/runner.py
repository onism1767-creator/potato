"""runner —— 本地向导的"跑测量"任务管理(2.3b):冻结配置 + 起 `aivis run` 子进程 + 状态轮询。

单用户单任务:同一时刻只允许一个 run。key 只放子进程 env(ANTHROPIC_API_KEY),**不落盘**。
真实 run 花钱/耗时(~25min),故异步:start 返回 job_id,前端轮询 status。
冻结:把内存 FrozenConfig dump 成 questions.yml + brands.yml(configio 能原样读回)。
"""

import os
import re
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from aivis.contracts.config import FrozenConfig
from aivis.gui import api

_BRAND_KEYS = {"competitor_set_version", "focal_domain", "peers", "competitors_on"}
_STAGES = ("probe", "extract", "verify", "score", "report")
_PROGRESS_RE = re.compile(r"^PROGRESS\s+probe\s+(\d+)/(\d+)(?:\s+([\d.]+))?")
# probe 是绝大部分耗时,占 0~85%;其余四段很快,各占完成时的固定点位
_STAGE_DONE_PERCENT = {"probe": 86, "extract": 90, "verify": 94, "score": 97, "report": 99}


def _run_command(*, config_dir: Path, data_dir: Path, provider: str, tier: str) -> list[str]:
    """构造 `aivis run` 子进程命令:`python -m aivis.cli ...`。

    便携包里 sys.executable 是官方 embeddable python.exe,同样适用。
    key 只走子进程 env、不进命令行(进程列表可见);tier 经命令行(非密钥,可见无妨)。
    """
    return [
        sys.executable,
        "-m",
        "aivis.cli",
        "--config-dir",
        str(config_dir),
        "--data-dir",
        str(data_dir),
        "--provider",
        provider,
        "--tier",
        tier,
        "run",
        "--yes",
    ]


def write_wizard_config(target: Path, cfg: FrozenConfig) -> None:
    """把 FrozenConfig 拆成 questions.yml + brands.yml(configio.load_frozen_config 可原样读回)。"""
    target.mkdir(parents=True, exist_ok=True)
    data = cfg.model_dump(mode="json")
    brands = {k: data[k] for k in _BRAND_KEYS}
    questions = {k: v for k, v in data.items() if k not in _BRAND_KEYS}
    (target / "questions.yml").write_text(
        yaml.safe_dump(questions, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    (target / "brands.yml").write_text(
        yaml.safe_dump(brands, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


@dataclass
class JobStatus:
    job_id: str
    state: str  # running | done | error
    stage: str = ""
    report_path: str | None = None
    error: str | None = None
    provider: str = ""  # mock | anthropic(前端据此提示成本)
    done: int = 0  # 已完成调用数(probe 进度)
    total: int = 0  # 调用总数上界
    spent_usd: float = 0.0  # 累计真实花费(mock 恒 0)
    started_monotonic: float = 0.0  # 起跑时刻(算 elapsed/eta)


class Runner:
    """单任务 run 管理(线程安全的最小版)。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._job: JobStatus | None = None
        self._proc: subprocess.Popen[str] | None = None

    def start(
        self,
        *,
        config_dir: Path,
        data_dir: Path,
        provider: str,
        tier: str,
        api_key: str | None,
    ) -> str:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                raise ValueError("已有测量任务在运行,请等它结束再开新的")
            env = dict(os.environ)
            if api_key:  # 🟥 只进子进程 env,不落盘、不进命令行(进程列表可见)
                env["ANTHROPIC_API_KEY"] = api_key
            env["PYTHONIOENCODING"] = "utf-8"  # 子进程按 UTF-8 输出(中文 Windows 默认 gbk 会崩)
            env["PYTHONUNBUFFERED"] = "1"  # 管道下不块缓冲,PROGRESS 行实时到达(进度条用)
            cmd = _run_command(
                config_dir=config_dir, data_dir=data_dir, provider=provider, tier=tier
            )
            proc = subprocess.Popen(
                cmd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",  # 🟥 两端钉死 UTF-8:中文 Windows 用 gbk 读子进程输出会崩
                errors="replace",
            )
            job = JobStatus(
                job_id=uuid.uuid4().hex,
                state="running",
                provider=provider,
                started_monotonic=time.monotonic(),
            )
            self._proc, self._job = proc, job
            threading.Thread(target=self._watch, args=(proc, job, data_dir), daemon=True).start()
            return job.job_id

    def _watch(self, proc: subprocess.Popen[str], job: JobStatus, data_dir: Path) -> None:
        tail: list[str] = []
        if proc.stdout is not None:
            for line in proc.stdout:
                s = line.strip()
                tail.append(s)
                m = _PROGRESS_RE.match(s)  # "PROGRESS probe d/t spent":probe 逐次进度+累计花费
                if m:
                    with self._lock:
                        job.stage = "probe"
                        job.done, job.total = int(m.group(1)), int(m.group(2))
                        if m.group(3) is not None:
                            job.spent_usd = float(m.group(3))
                    continue
                for stage in _STAGES:  # "probe 完成 / extract 完成 …":阶段推进
                    if s.startswith(stage):
                        with self._lock:
                            job.stage = stage
        rc = proc.wait()
        with self._lock:
            if rc != 0:
                job.state = "error"
                job.error = f"run 失败(退出码 {rc}):" + " ".join(tail[-3:])
                return
            report = _find_latest_report(data_dir)
            if report is None:
                job.state = "error"
                job.error = "run 完成但未找到 report.html"
            else:
                job.state = "done"
                job.report_path = str(report)

    def status(self) -> dict[str, Any]:
        with self._lock:
            if self._job is None:
                return {"state": "idle"}
            j = self._job
            elapsed = time.monotonic() - j.started_monotonic if j.started_monotonic else 0.0
            percent, eta = _progress(j, elapsed)
            return {
                "job_id": j.job_id,
                "state": j.state,
                "stage": j.stage,
                "report_path": j.report_path,
                "error": j.error,
                "provider": j.provider,
                "done": j.done,
                "total": j.total,
                "spent_usd": round(j.spent_usd, 4),
                "elapsed_s": round(max(0.0, elapsed)),
                "percent": percent,
                "eta_s": eta,
            }


def _progress(job: JobStatus, elapsed: float) -> tuple[int | None, int | None]:
    """从 job 状态算总进度百分比 + 预估剩余秒数(给 GUI 进度条/ETA)。

    probe 阶段按已完成/总数映射到 0~85%,ETA 用当前调用速率外推;其余四段很快,
    完成时跳到固定点位、剩余秒数置 None(前端显示"收尾中")。done/error 各返回终态。
    """
    if job.state == "done":
        return 100, 0
    if job.state == "error":
        return None, None
    if job.stage in ("", "probe"):
        frac = (job.done / job.total) if job.total else 0.0
        percent = round(min(1.0, frac) * 85)  # probe 占总进度 0~85%(其余四段占 85~100)
        if job.done > 0 and job.total:
            rate = elapsed / job.done  # 秒/次
            eta = round(rate * max(0, job.total - job.done))
            return percent, eta
        return percent, None
    return _STAGE_DONE_PERCENT.get(job.stage, 88), None


def _find_latest_report(data_dir: Path) -> Path | None:
    analysis = data_dir / "analysis"
    if not analysis.exists():
        return None
    reports = sorted(analysis.glob("*/report.html"), key=lambda p: p.stat().st_mtime)
    return reports[-1] if reports else None


def start_run(runner: Runner, payload: dict[str, Any], work_root: Path) -> dict[str, Any]:
    """冻结配置 + 起 run 子进程。anthropic 档必须带 api_key(只入子进程 env)。"""
    questions = api._questions_from(payload)
    cfg = api._config_from(payload, questions)
    provider = api._str(payload, "provider") or "mock"
    api_key = api._str(payload, "api_key") or None
    if provider == "anthropic" and not api_key:
        raise ValueError("anthropic 档需要 api_key(只在本机内存、传子进程 env、不落盘)")
    config_dir, data_dir = work_root / "config", work_root / "data"
    write_wizard_config(config_dir, cfg)
    job_id = runner.start(
        config_dir=config_dir,
        data_dir=data_dir,
        provider=provider,
        tier=cfg.tier,  # GUI 选的档位经命令行传给 run(否则用默认 standard)
        api_key=api_key,
    )
    return {"job_id": job_id, "state": "running"}
