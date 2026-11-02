"""Reporter：合并所有步骤的结论与引用，生成最终报告。"""

from __future__ import annotations

import logging

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from ..config import get_settings
from ..llm import get_llm
from ..prompts import render_prompt
from ..references import build_global_references, format_references_for_prompt, render_reference_list, validate_citations
from ..services import memory, storage
from ..state import State
from .common import emit, sorted_results

logger = logging.getLogger(__name__)


async def reporter_node(state: State, config: RunnableConfig) -> dict:
    settings = get_settings(config)
    locale = state.get("locale") or settings.locale
    results = sorted_results(state.get("step_results", {}))
    refs, rewritten = build_global_references(results)

    plan = state.get("current_plan")
    findings = "\n".join(
        f"## {r.title}\n\n<finding>\n{rewritten[r.step_id]}\n</finding>\n" for r in results
    ) or "（没有收集到研究结果）"
    user_content = (
        f"# Research Requirements\n\n## Task\n{state.get('research_topic', '')}\n\n"
        f"## Plan Title\n{plan.title if plan else ''}\n\n"
        f"# Research Findings\n\n{findings}\n\n"
        f"# Available References\n\n{format_references_for_prompt(refs, with_content=False) or '（无）'}\n"
    )
    messages = [SystemMessage(render_prompt("reporter", locale=locale)), HumanMessage(user_content)]
    response = await get_llm("reporter", settings).ainvoke(messages)
    report = response.content if isinstance(response.content, str) else str(response.content)

    if settings.validate_citations:
        report, dropped = validate_citations(report, len(refs))
        if dropped:
            logger.info("reporter: dropped %d out-of-range citations", dropped)
    report = report.rstrip() + "\n" + render_reference_list(report, refs)

    thread_id = (config.get("configurable") or {}).get("thread_id")
    if thread_id:
        storage.save_report(thread_id, state.get("research_topic", ""), report, {"references": len(refs)})
        memory.remember(thread_id, state.get("research_topic", ""), report)

    emit("reporter_output", content=report)
    return {"final_report": report, "report_references": refs, "messages": [AIMessage(report, name="reporter")]}
