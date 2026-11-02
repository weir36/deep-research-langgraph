"""真实进程崩溃：子进程跑到步骤 B 的 researcher_output 时被 SIGKILL，父进程从同一个 SQLite 恢复。

和 test_graph.py 里抛异常的版本不同，这里进程直接消失，没有任何清理代码执行，
只有已经落盘的 checkpoint 能用。"""

import os
import signal
import subprocess
import sys
import textwrap
from pathlib import Path

from deep_research.runner import open_graph, run

TOPIC = "世界最高建筑比埃菲尔铁塔高多少倍？"
ROOT = Path(__file__).resolve().parents[1]

CHILD = textwrap.dedent(
    """
    import asyncio, os, signal, sys, tempfile
    os.environ["DR_DATA_DIR"] = tempfile.mkdtemp(prefix="dr_kill_")
    os.environ.pop("TAVILY_API_KEY", None)

    from deep_research.llm import set_llm_factory
    from deep_research.runner import open_graph, run
    from deep_research.tools.search import MockBackend, set_search_backend
    from tests.fakes import FakeWorld

    world = FakeWorld()

    def kill_on_step_b(messages):
        if "步骤B" in messages[-1].content:
            os.kill(os.getpid(), signal.SIGKILL)

    world.hooks["researcher_output"] = kill_on_step_b
    set_llm_factory(lambda node, settings: world.model)
    set_search_backend(MockBackend())

    async def main():
        async with open_graph(sys.argv[1]) as graph:
            await run(graph, query=sys.argv[2], thread_id="killed", configurable={"auto_accept_plan": True})

    asyncio.run(main())
    """
)


async def test_resume_after_sigkill(world, tmp_path):
    db = tmp_path / "cp.sqlite"
    proc = subprocess.run(
        [sys.executable, "-c", CHILD, str(db), TOPIC], cwd=ROOT, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == -signal.SIGKILL, proc.stderr

    # 新进程（本测试进程）：只靠 SQLite 里的 checkpoint 继续
    async with open_graph(db) as graph:
        done = await run(graph, thread_id="killed")

    assert "## 参考文献" in done.final_report
    assert set(done.state["step_results"]) == {"p1s1", "p1s2", "p1s3"}
    # 被杀之前已完成的工作不重做：步骤 B 的搜索已落盘，不再搜索
    assert world.calls[("researcher", "步骤B 埃菲尔铁塔")] == 0
    assert world.calls[("researcher_output", "步骤B 埃菲尔铁塔")] == 1
