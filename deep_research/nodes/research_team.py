"""Research Team：按依赖关系分发步骤。

依赖已满足的步骤会同时通过 Send 分发出去并行执行；全部完成后回到 Planner。
"""

from __future__ import annotations

from langchain_core.runnables import RunnableConfig
from langgraph.types import Send

from ..config import get_settings
from ..schemas import Plan, StepResult, StepTask, StepType, step_id
from ..state import State
from .common import emit


def research_team_node(state: State) -> dict:
    return {}


def effective_deps(plan: Plan, index: int) -> set[int]:
    """返回 0-based 依赖下标。非法编号会被忽略；processing 步骤未声明依赖时，默认依赖它之前的所有步骤。"""
    step = plan.steps[index]
    deps = {d - 1 for d in step.depends_on if 1 <= d <= len(plan.steps) and d - 1 != index}
    if not deps and step.step_type == StepType.PROCESSING:
        deps = set(range(index))
    return deps


def ready_steps(plan: Plan, iteration: int, done: set[str]) -> list[int]:
    pending = [i for i in range(len(plan.steps)) if step_id(iteration, i) not in done]
    ready = [i for i in pending if all(step_id(iteration, d) in done for d in effective_deps(plan, i))]
    if pending and not ready:  # 依赖成环：退化为顺序执行
        ready = pending[:1]
    return ready


def route_research_team(state: State, config: RunnableConfig) -> list[Send] | str:
    settings = get_settings(config)
    plan = state.get("current_plan")
    iteration = state.get("plan_iterations", 0)
    results: dict[str, StepResult] = state.get("step_results", {})
    if not plan or not plan.steps:
        return "planner"

    ready = ready_steps(plan, iteration, set(results))
    if not ready:
        return "planner"
    if not settings.parallel_steps:
        ready = ready[:1]

    # 可参考的上下文：前几轮的全部结果 + 本轮依赖步骤的结果（不随并发时序变化，保证可复现）
    previous = [r for r in results.values() if r.iteration < iteration]
    sends = []
    for i in ready:
        deps = [results[step_id(iteration, d)] for d in sorted(effective_deps(plan, i))]
        step = plan.steps[i]
        task = StepTask(
            step_id=step_id(iteration, i),
            iteration=iteration,
            index=i,
            step=step,
            research_topic=plan.title or state.get("research_topic", ""),
            locale=state.get("locale") or settings.locale,
            context=sorted(previous, key=lambda r: (r.iteration, r.index)) + deps,
            resources=state.get("resources") or [],
        )
        target = "coder" if step.step_type == StepType.PROCESSING else "research_step"
        sends.append(Send(target, {"task": task}))
    emit("research_team", action="dispatch", steps=[s.arg["task"].step_id for s in sends])
    return sends
