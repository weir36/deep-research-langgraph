"""Coordinator：意图识别。闲聊 / 简单问题直接回答，研究类问题交给 Planner。"""

from __future__ import annotations

from typing import Literal

from langchain_core.messages import SystemMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.graph import END
from langgraph.types import Command

from ..config import get_settings
from ..llm import get_llm
from ..prompts import render_prompt
from ..state import State
from .common import emit, last_human_text


@tool
def handoff_to_planner(research_topic: str, locale: str) -> str:
    """Hand off a research task to the planner.

    Args:
        research_topic: The topic of the research task, in the user's language.
        locale: The user's language locale, e.g. zh-CN or en-US.
    """
    return "handed off"


async def coordinator_node(state: State, config: RunnableConfig) -> Command[Literal["planner", "__end__"]]:
    settings = get_settings(config)
    llm = get_llm("coordinator", settings).bind_tools([handoff_to_planner])
    messages = [SystemMessage(render_prompt("coordinator", locale=settings.locale)), *state["messages"]]
    response = await llm.ainvoke(messages)

    calls = [c for c in response.tool_calls if c["name"] == "handoff_to_planner"]
    if calls:
        # 研究主题用用户原话：LLM 改写可能丢掉限定条件
        topic = last_human_text(state["messages"]) or calls[0]["args"].get("research_topic", "")
        emit("coordinator", action="handoff", research_topic=topic)
        return Command(
            goto="planner",
            update={"research_topic": topic, "locale": calls[0]["args"].get("locale") or settings.locale},
        )

    emit("coordinator", action="reply", content=response.content)
    return Command(goto=END, update={"messages": [response], "final_report": response.content})
