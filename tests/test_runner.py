from deep_research.runner import list_threads, open_graph, run
from deep_research.services.storage import load_report

TOPIC = "世界最高建筑比埃菲尔铁塔高多少倍？"


async def test_sqlite_persistence_and_resume(world, tmp_path):
    db = tmp_path / "cp.sqlite"
    # 第一个"进程"：生成计划后无人审核，任务挂起
    async with open_graph(db) as graph:
        first = await run(graph, query=TOPIC, thread_id="job1")
        assert first.final_report == ""
        assert [t["thread_id"] for t in await list_threads(graph)] == ["job1"]

    # 第二个"进程"：从 SQLite 读回挂起的任务，审核后跑完
    async def review(payload):
        assert payload["type"] == "plan_review"
        return "ACCEPT"

    async with open_graph(db) as graph:
        done = await run(graph, thread_id="job1", review=review)
        assert "## 参考文献" in done.final_report
        assert await list_threads(graph) == []
    assert load_report("job1") == done.final_report
