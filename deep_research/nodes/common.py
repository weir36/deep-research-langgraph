from __future__ import annotations

from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage
from langgraph.config import get_stream_writer

from ..references import strip_citations
from ..schemas import StepResult


def emit(event: str, **data: Any) -> None:
    """推送自定义事件（对应时序图中的 tool act / tool resp / xxx output）。"""
    try:
        writer = get_stream_writer()
    except RuntimeError:  # 不在图的运行上下文中
        return
    writer({"event": event, **data})


def last_human_text(messages: list[BaseMessage]) -> str:
    for msg in reversed(messages):
        if isinstance(msg, HumanMessage):
            return msg.content if isinstance(msg.content, str) else str(msg.content)
    return ""


def format_findings(results: list[StepResult], *, heading: str = "Completed Research Steps") -> str:
    """已完成步骤的结论。去掉 [[n]]：它们是其他步骤的局部编号，带过去会造成引用错位。"""
    if not results:
        return ""
    parts = [f"# {heading}\n"]
    for i, r in enumerate(results, start=1):
        parts.append(f"## Completed Step {i}: {r.title}\n\n<finding>\n{strip_citations(r.execution_res)}\n</finding>\n")
    return "\n".join(parts)


def sorted_results(step_results: dict[str, StepResult]) -> list[StepResult]:
    return sorted(step_results.values(), key=lambda r: (r.iteration, r.index))
