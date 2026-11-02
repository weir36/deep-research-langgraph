"""Planner：生成研究计划，判断信息是否充足。"""

from __future__ import annotations

import logging
from typing import Literal

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.types import Command

from ..config import get_settings
from ..llm import get_llm
from ..prompts import render_prompt
from ..schemas import Plan
from ..state import State
from .common import emit, format_findings, sorted_results

logger = logging.getLogger(__name__)


async def planner_node(state: State, config: RunnableConfig) -> Command[Literal["human_feedback", "research_team", "reporter"]]:
    settings = get_settings(config)
    iterations = state.get("plan_iterations", 0)

    # 达到最大轮数：不再调用 LLM，直接写报告
    if iterations >= settings.max_plan_iterations:
        emit("planner", action="max_iterations", plan_iterations=iterations)
        return Command(goto="reporter")

    results = sorted_results(state.get("step_results", {}))
    user_content = f"# Research Topic\n{state['research_topic']}\n"
    if results:
        user_content += "\n" + format_findings(results, heading="Findings From Previous Rounds")
    messages = [
        SystemMessage(render_prompt(
            "planner",
            locale=state.get("locale") or settings.locale,
            max_step_num=settings.max_step_num,
            has_findings=bool(results),
        )),
        HumanMessage(user_content),
    ]

    llm = get_llm("planner", settings).with_structured_output(Plan, method="function_calling")
    plan: Plan | None = None
    for attempt in range(2):
        try:
            plan = await llm.ainvoke(messages)
            if plan is not None:
                break
        except Exception as exc:  # 结构化输出解析失败，重试一次
            logger.warning("planner output parse failed (attempt %d): %s", attempt + 1, exc)
    if plan is None:
        if results:  # 已有研究结果，降级为直接写报告
            return Command(goto="reporter")
        raise RuntimeError("Planner 无法生成有效计划")

    plan = plan.model_copy(update={"steps": plan.steps[: settings.max_step_num]})
    emit("planner", action="plan", plan=plan.model_dump(mode="json"), plan_iterations=iterations + 1)
    update = {"current_plan": plan, "plan_iterations": iterations + 1, "locale": plan.locale or settings.locale}

    if plan.has_enough_context or not plan.steps:
        return Command(goto="reporter", update=update)
    # 只有首轮计划需要用户审核，补充轮直接执行
    if iterations == 0 and not settings.auto_accept_plan:
        return Command(goto="human_feedback", update=update)
    return Command(goto="research_team", update=update)
