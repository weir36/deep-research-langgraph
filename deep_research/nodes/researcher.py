"""研究步骤子图：researcher（调工具收集引用）→ researcher_output（整合结论）。

每个 research 步骤以独立子图实例运行，多个步骤可并行；子图有自己的 checkpoint 命名空间，
researcher_output 失败重试时不会重新搜索。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TypedDict

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.graph import END, START, StateGraph

from ..config import Settings, get_settings
from ..grounding import prune_unsupported
from ..llm import get_llm
from ..prompts import render_prompt
from ..references import format_references_for_prompt, select_excerpt, validate_citations
from ..schemas import Reference, StepResult, StepTask
from ..tools.knowledge import has_knowledge, knowledge_search as kb_search
from ..tools.search import SearchBudgetExhausted, get_search_backend
from .common import emit, format_findings

logger = logging.getLogger(__name__)

MAX_REFS_PER_STEP = 15


class StepState(TypedDict, total=False):
    task: StepTask
    references: list[Reference]
    step_results: dict[str, StepResult]


class StepInput(TypedDict):
    task: StepTask


class StepOutput(TypedDict):
    step_results: dict[str, StepResult]


# 只用于向 LLM 声明工具 schema，实际执行在 _run_tool 中（需要访问步骤内的引用列表）
@tool
def web_search(query: str) -> str:
    """Search the web. Use specific, keyword-rich queries."""
    raise NotImplementedError


@tool
def knowledge_search(query: str) -> str:
    """Search the user's local knowledge base and uploaded files."""
    raise NotImplementedError


@tool
def handoff_to_researcher_output() -> str:
    """Hand off to the researcher_output agent when enough information is collected."""
    raise NotImplementedError


class _Collector:
    """单个步骤内的引用收集器：按 URL / 文档片段去重，编号即列表位置。"""

    def __init__(self, task: StepTask, settings: Settings) -> None:
        self.task = task
        self.settings = settings
        self.refs: list[Reference] = []
        self._seen: set[str] = set()

    def _add(self, ref: Reference) -> bool:
        if ref.key in self._seen or len(self.refs) >= MAX_REFS_PER_STEP:
            return False
        self._seen.add(ref.key)
        self.refs.append(ref)
        return True

    async def web(self, query: str) -> str:
        hits = await get_search_backend().search(query, self.settings.max_search_results)
        rerank_query = f"{query} {self.task.step.title}"
        lines = []
        for hit in hits:
            content = select_excerpt(
                rerank_query, hit.snippet, hit.raw_content,
                strategy=self.settings.context_strategy,
                budget=self.settings.ref_char_budget,
                chunk_size=self.settings.chunk_size,
            )
            added = self._add(Reference(kind="web", title=hit.title, url=hit.url, content=content, score=hit.score))
            lines.append(f"- {'(duplicate) ' if not added else ''}{hit.title}: {hit.snippet[:150]}")
        return "\n".join(lines) or "No results. Try different keywords."

    async def knowledge(self, query: str) -> str:
        refs = kb_search(query, self.task.resources)
        lines = [f"- {'' if self._add(r) else '(duplicate) '}{r.title}: {r.content[:150]}" for r in refs]
        return "\n".join(lines) or "No relevant documents in the knowledge base."


async def _run_tool(collector: _Collector, call: dict) -> ToolMessage:
    name, args = call["name"], call.get("args") or {}
    query = str(args.get("query") or "").strip()
    emit("tool_act", agent="researcher", step_id=collector.task.step_id, tool=name, args=args)
    try:
        if name == "handoff_to_researcher_output":
            content = "OK"
        elif not query:
            content = "Error: query must not be empty."
        elif name == "web_search":
            content = await collector.web(query)
        elif name == "knowledge_search":
            content = await collector.knowledge(query)
        else:
            content = f"Error: unknown tool {name}"
    except SearchBudgetExhausted:
        raise  # 额度用完：整次研究失败，不能在 0 个来源上继续写报告
    except Exception as exc:  # 工具失败不终止步骤，把错误交给 LLM 换个方式重试
        logger.warning("tool %s failed: %s", name, exc)
        content = f"Error: {exc}"
    emit("tool_resp", agent="researcher", step_id=collector.task.step_id, tool=name, refs=len(collector.refs))
    return ToolMessage(content=content, tool_call_id=call["id"], name=name)


def _step_brief(task: StepTask) -> str:
    return (
        f"# Research Topic\n{task.research_topic}\n\n"
        f"{format_findings(task.context)}\n"
        f"# Current Step\n## Title\n{task.step.title}\n\n## Description\n{task.step.description}\n\n"
        f"## Locale\n{task.locale}\n"
    )


async def researcher_node(state: StepState, config: RunnableConfig) -> dict:
    task, settings = state["task"], get_settings(config)
    data_sources = ["web"] + (["knowledge_base"] if has_knowledge(task.resources) else [])
    tools = [handoff_to_researcher_output]
    if "web" in data_sources:
        tools.append(web_search)
    if "knowledge_base" in data_sources:
        tools.append(knowledge_search)
    llm = get_llm("researcher", settings).bind_tools(tools)

    messages = [
        SystemMessage(render_prompt("researcher", locale=task.locale, data_sources=data_sources)),
        HumanMessage(_step_brief(task)),
    ]
    collector = _Collector(task, settings)
    for _ in range(settings.max_research_rounds):
        response: AIMessage = await llm.ainvoke(messages)
        messages.append(response)  # 每轮只追加一次 AI 消息
        if not response.tool_calls:
            break
        # 同一轮的多个工具调用并发执行，且每个 tool_call 都有对应的 ToolMessage
        messages.extend(await asyncio.gather(*(_run_tool(collector, c) for c in response.tool_calls)))
        if any(c["name"] == "handoff_to_researcher_output" for c in response.tool_calls):
            break

    if not collector.refs:  # LLM 一次都没搜：用步骤标题兜底搜一次
        await _run_tool(collector, {"name": "web_search", "args": {"query": task.step.title}, "id": "fallback"})
    return {"references": collector.refs}


async def researcher_output_node(state: StepState, config: RunnableConfig) -> dict:
    task, settings = state["task"], get_settings(config)
    refs = state.get("references", [])
    refs_block = (
        "# Current Step References\n\n" + format_references_for_prompt(refs)
        if refs else "# Current Step References\n\n未收集到任何引用"
    )
    messages = [
        SystemMessage(render_prompt("researcher_output", locale=task.locale)),
        HumanMessage(f"{_step_brief(task)}\n{refs_block}\n\nPlease synthesize the information and provide a structured conclusion."),
    ]
    response = await get_llm("researcher_output", settings).ainvoke(messages)
    content = response.content if isinstance(response.content, str) else str(response.content)
    if settings.validate_citations:
        content, dropped = validate_citations(content, len(refs))
        if dropped:
            logger.info("step %s: dropped %d out-of-range citations", task.step_id, dropped)
    if settings.ground_citations and refs:
        content, pruned = await prune_unsupported(content, refs, settings)
        if pruned:
            logger.info("step %s: pruned %d unsupported citations", task.step_id, pruned)

    result = StepResult(
        step_id=task.step_id, iteration=task.iteration, index=task.index,
        title=task.step.title, description=task.step.description, step_type=task.step.step_type,
        execution_res=content, references=refs,
    )
    emit("researcher_output", step_id=task.step_id, title=task.step.title, content=content, references=len(refs))
    return {"step_results": {task.step_id: result}}


def build_research_subgraph():
    builder = StateGraph(StepState, input_schema=StepInput, output_schema=StepOutput)
    builder.add_node("researcher", researcher_node)
    builder.add_node("researcher_output", researcher_output_node)
    builder.add_edge(START, "researcher")
    builder.add_edge("researcher", "researcher_output")
    builder.add_edge("researcher_output", END)
    return builder.compile()
