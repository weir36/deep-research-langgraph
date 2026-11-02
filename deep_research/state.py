"""LangGraph 全局状态。

与参考实现的区别：节点不原地修改状态对象，只返回增量更新，由 reducer 合并。
这样并行分支的结果能安全合并，checkpoint 中的每个版本也是不可变快照，可以断点恢复。
"""

from __future__ import annotations

from typing import Annotated

from langgraph.graph import MessagesState

from .schemas import Plan, Reference, StepResult


def merge_dicts(left: dict | None, right: dict | None) -> dict:
    return {**(left or {}), **(right or {})}


class State(MessagesState):
    research_topic: str
    locale: str
    resources: list[str]  # 用户上传的文件路径（对应时序图中的临时知识库）
    current_plan: Plan | None
    plan_iterations: int  # 已生成的计划轮数
    step_results: Annotated[dict[str, StepResult], merge_dicts]  # step_id -> 结果，跨轮累积
    final_report: str
    report_references: list[Reference]
