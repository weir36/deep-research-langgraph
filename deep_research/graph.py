"""LangGraph 状态图构建。

    START → coordinator ─┬→ END（闲聊 / 简单问题）
                         └→ planner ─┬→ human_feedback ─┐（仅首轮计划）
                                     ├→ reporter → END   │
                                     └→ research_team ←──┘
                                          │  ▲  按依赖并行分发（Send）
                                          ▼  │
                              research_step（子图：researcher → researcher_output）
                              coder
                                          │ 全部完成
                                          └→ planner（评估是否需要补充研究）
"""

from __future__ import annotations

from langgraph.graph import START, END, StateGraph

from .nodes.coder import coder_node
from .nodes.coordinator import coordinator_node
from .nodes.human_feedback import human_feedback_node
from .nodes.planner import planner_node
from .nodes.reporter import reporter_node
from .nodes.research_team import research_team_node, route_research_team
from .nodes.researcher import build_research_subgraph
from .state import State


def build_graph(checkpointer=None):
    builder = StateGraph(State)
    builder.add_node("coordinator", coordinator_node)
    builder.add_node("planner", planner_node)
    builder.add_node("human_feedback", human_feedback_node)
    builder.add_node("research_team", research_team_node)
    builder.add_node("research_step", build_research_subgraph())
    builder.add_node("coder", coder_node)
    builder.add_node("reporter", reporter_node)

    builder.add_edge(START, "coordinator")
    # coordinator / planner / human_feedback 通过 Command(goto=...) 路由
    builder.add_conditional_edges("research_team", route_research_team, ["research_step", "coder", "planner"])
    builder.add_edge("research_step", "research_team")
    builder.add_edge("coder", "research_team")
    builder.add_edge("reporter", END)
    return builder.compile(checkpointer=checkpointer)
