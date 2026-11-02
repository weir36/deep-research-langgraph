"""Coder 的 Python 执行沙箱。

每次执行都是独立子进程：超时强杀、限制 CPU 时间和写文件大小、临时工作目录、
清空环境变量（不泄露 API Key）、默认禁用 socket。
注意：这是进程级的隔离，不是安全边界；生产环境应放进容器 / gVisor / Firecracker。
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

MAX_OUTPUT = 8000

_RUNNER = r'''
import runpy, sys
if sys.argv[2] != "1":
    import socket
    def _blocked(*a, **k):
        raise OSError("network access is disabled in the sandbox")
    socket.socket.connect = _blocked
    socket.socket.connect_ex = _blocked
    socket.getaddrinfo = _blocked
sys.argv = [sys.argv[1]]
runpy.run_path(sys.argv[0], run_name="__main__")
'''


def _limits(cpu_seconds: int):
    def apply() -> None:
        import resource

        for limit, value in (
            (resource.RLIMIT_CPU, cpu_seconds),
            (resource.RLIMIT_FSIZE, 50 * 1024 * 1024),
        ):
            try:
                resource.setrlimit(limit, (value, value))
            except (ValueError, OSError):
                pass

    return apply


def _clip(text: str) -> str:
    return text if len(text) <= MAX_OUTPUT else text[:MAX_OUTPUT] + f"\n...（输出过长，已截断 {len(text) - MAX_OUTPUT} 字符）"


async def run_python(code: str, *, timeout: int = 30, allow_network: bool = False) -> str:
    with tempfile.TemporaryDirectory(prefix="dr_sandbox_") as tmp:
        runner = Path(tmp) / "_runner.py"
        script = Path(tmp) / "main.py"
        runner.write_text(_RUNNER, encoding="utf-8")
        script.write_text(code, encoding="utf-8")
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-I", str(runner), str(script), "1" if allow_network else "0",
            cwd=tmp,
            env={"PATH": "/usr/bin:/bin", "PYTHONIOENCODING": "utf-8", "MPLBACKEND": "Agg"},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            preexec_fn=_limits(timeout),
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return f"执行超时（>{timeout}s），进程已终止。"
    out, err = stdout.decode(errors="replace"), stderr.decode(errors="replace")
    if proc.returncode != 0:
        return _clip(f"执行失败（exit code {proc.returncode}）\nStdout: {out}\nStderr: {err}")
    return _clip(f"Successfully executed.\nStdout: {out}" + (f"\nStderr: {err}" if err.strip() else ""))
