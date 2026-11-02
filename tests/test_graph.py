import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from deep_research.graph import build_graph
from deep_research.llm import UsageTracker

from .fakes import DEFAULT_PLAN, edit_plan_payload

TOPIC = "世界最高建筑比埃菲尔铁塔高多少倍？"


def _config(thread="t1", **configurable):
    return {"configurable": {"thread_id": thread, **configurable}}


async def _run(graph, inputs, config):
    events = []
    async for _ns, mode, chunk in graph.astream(inputs, config, stream_mode=["updates", "custom"], subgraphs=True):
        if mode == "custom":
            events.append(chunk)
    return events


async def test_small_talk_ends_at_coordinator(world):
    graph = build_graph()
    out = await graph.ainvoke({"messages": [HumanMessage("你好")]}, _config())
    assert "领域智能体" in out["final_report"]
    assert ("planner", "") not in world.calls


async def test_full_flow_parallel(world):
    graph = build_graph(InMemorySaver())
    tracker = UsageTracker()
    config = {**_config(auto_accept_plan=True, ground_citations=True), "callbacks": [tracker]}
    events = await _run(graph, {"messages": [HumanMessage(TOPIC)]}, config)
    state = (await graph.aget_state(config)).values

    dispatches = [e["steps"] for e in events if e["event"] == "research_team"]
    assert dispatches == [["p1s1", "p1s2"], ["p1s3"]]  # A、B 并行，C 等待依赖

    results = state["step_results"]
    assert set(results) == {"p1s1", "p1s2", "p1s3"}
    assert "[[99]]" not in results["p1s1"].execution_res  # 步骤内越界引用被清理
    assert "编造的推断[[3]]" not in results["p1s1"].execution_res  # 开启剪枝时，来源不支持的引用被剪掉
    assert "关键数据[[1]][[2]]" in results["p1s1"].execution_res  # 被支持的引用保留
    assert "2.50909" in results["p1s3"].execution_res  # coder 在沙箱中真实执行

    report = state["final_report"]
    assert "[[50]]" not in report
    assert "## 参考文献" in report and "[4]" in report
    assert len(state["report_references"]) == 12  # 2 步 × 2 次搜索 × 3 条结果，URL 不重复

    usage = tracker.summary()
    assert usage["by_node"]["researcher"]["calls"] == 4
    assert usage["total"]["cost_usd"] > 0


async def test_grounding_off_by_default(world):
    graph = build_graph(InMemorySaver())
    out = await graph.ainvoke({"messages": [HumanMessage(TOPIC)]}, _config(auto_accept_plan=True))
    assert "编造的推断[[3]]" in out["step_results"]["p1s1"].execution_res
    assert world.calls[("grounding", "")] == 0


async def test_sequential_mode(world):
    graph = build_graph(InMemorySaver())
    events = await _run(graph, {"messages": [HumanMessage(TOPIC)]}, _config(auto_accept_plan=True, parallel_steps=False))
    dispatches = [e["steps"] for e in events if e["event"] == "research_team"]
    assert dispatches == [["p1s1"], ["p1s2"], ["p1s3"]]


async def test_human_feedback_edit_plan(world):
    graph = build_graph(InMemorySaver())
    config = _config("edit")
    await graph.ainvoke({"messages": [HumanMessage(TOPIC)]}, config)
    snapshot = await graph.aget_state(config)
    assert snapshot.next == ("human_feedback",)
    assert snapshot.tasks[0].interrupts[0].value["plan"]["title"] == DEFAULT_PLAN["title"]

    # 前端只能编辑标题和描述：新增的步骤没有 step_type / need_search
    edited = {"steps": [{"title": "步骤X 用户新增", "description": "用户自己加的步骤"}]}
    out = await graph.ainvoke(Command(resume=edit_plan_payload(edited)), config)
    assert set(out["step_results"]) == {"p1s1"}
    assert out["step_results"]["p1s1"].title == "步骤X 用户新增"
    assert world.calls[("researcher", "步骤B 埃菲尔铁塔")] == 0


async def test_followup_round_and_iteration_cap(world):
    world.followup_plan = {
        **DEFAULT_PLAN, "steps": [{"title": "步骤D 补充", "description": "补充研究", "step_type": "research"}],
    }
    graph = build_graph(InMemorySaver())
    out = await graph.ainvoke({"messages": [HumanMessage(TOPIC)]}, _config(auto_accept_plan=True, max_plan_iterations=2))
    assert out["plan_iterations"] == 2
    assert "p2s1" in out["step_results"]  # 第二轮计划被执行，而不是一生成就去写报告
    assert world.calls[("planner", "")] == 2  # 第三次进入 planner 时直接跳 reporter，不调 LLM


async def test_resume_after_crash_does_not_redo_finished_work(world):
    """步骤 B 的 researcher_output 第一次调用时崩溃，恢复后：
    - A、B 的 researcher（子图中已完成的节点）都不重新搜索 —— 搜索是最贵、也最不该重复的部分；
    - 同一超步里还没跑完的兄弟任务（A 的 researcher_output 正在做引用核验）会被取消，恢复时重跑。
      LangGraph 只保存已完成任务的写入，这是预期语义。"""
    crashed = {"done": False}

    def crash_once(messages):
        if "步骤B" in messages[-1].content and not crashed["done"]:
            crashed["done"] = True
            raise RuntimeError("模拟进程崩溃 / 模型接口超时")

    world.hooks["researcher_output"] = crash_once
    graph = build_graph(InMemorySaver())
    config = _config("crash", auto_accept_plan=True)
    with pytest.raises(RuntimeError):
        await graph.ainvoke({"messages": [HumanMessage(TOPIC)]}, config)

    out = await graph.ainvoke(None, config)  # 从断点继续
    assert "## 参考文献" in out["final_report"]
    assert world.calls[("researcher", "步骤A 世界最高建筑")] == 2
    assert world.calls[("researcher_output", "步骤A 世界最高建筑")] <= 2
    assert world.calls[("researcher", "步骤B 埃菲尔铁塔")] == 2
    assert world.calls[("researcher_output", "步骤B 埃菲尔铁塔")] == 2
