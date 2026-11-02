"""Human Feedback：挂起图的执行，等待客户端确认或修改计划。

客户端通过 Command(resume=...) 恢复，支持：
- "ACCEPT"
- "EDIT PLAN" + 修改后的计划 JSON（字符串），或直接传 dict
"""

from __future__ import annotations

import json
from typing import Any, Literal

from langgraph.types import Command, interrupt

from ..schemas import Plan
from ..state import State
from .common import emit


def parse_feedback(feedback: Any, current: Plan) -> Plan:
    if isinstance(feedback, dict):
        return Plan.model_validate({**current.model_dump(), **feedback})
    text = str(feedback or "").strip()
    if not text or text.upper().startswith("ACCEPT"):
        return current
    if text.upper().startswith("EDIT PLAN"):
        payload = text[len("EDIT PLAN"):].strip()
        return Plan.model_validate({**current.model_dump(), **json.loads(payload)})
    raise ValueError(f"无法识别的反馈: {text[:50]}")


def human_feedback_node(state: State) -> Command[Literal["research_team", "reporter"]]:
    plan = state["current_plan"]
    feedback = interrupt({"type": "plan_review", "plan": plan.model_dump(mode="json")})
    plan = parse_feedback(feedback, plan)
    emit("human_feedback", action="accepted", plan=plan.model_dump(mode="json"))
    return Command(goto="research_team" if plan.steps else "reporter", update={"current_plan": plan})
