"""Coder：在沙箱中执行 Python 完成计算类步骤。"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from ..config import get_settings
from ..llm import get_llm
from ..prompts import render_prompt
from ..schemas import StepResult, StepTask
from ..tools.sandbox import run_python
from .common import emit, format_findings


@tool
def python_repl_tool(code: str) -> str:
    """Execute Python code in an isolated sandbox. Use print(...) to output values. State does not persist between calls."""
    raise NotImplementedError


async def coder_node(state: dict, config: RunnableConfig) -> dict:
    task: StepTask = state["task"]
    settings = get_settings(config)
    llm = get_llm("coder", settings)
    llm_with_tools = llm.bind_tools([python_repl_tool])

    messages = [
        SystemMessage(render_prompt(
            "coder", locale=task.locale, timeout=settings.coder_timeout, allow_network=settings.coder_allow_network,
        )),
        HumanMessage(
            f"# Research Topic\n{task.research_topic}\n\n{format_findings(task.context)}\n"
            f"# Current Step\n## Title\n{task.step.title}\n\n## Description\n{task.step.description}\n\n## Locale\n{task.locale}\n"
        ),
    ]
    response: AIMessage | None = None
    for _ in range(settings.max_coder_rounds):
        response = await llm_with_tools.ainvoke(messages)
        messages.append(response)
        if not response.tool_calls:
            break
        for call in response.tool_calls:
            code = (call.get("args") or {}).get("code", "")
            emit("tool_act", agent="coder", step_id=task.step_id, tool=call["name"], args={"code": code})
            output = await run_python(code, timeout=settings.coder_timeout, allow_network=settings.coder_allow_network)
            emit("tool_resp", agent="coder", step_id=task.step_id, tool=call["name"], output=output)
            messages.append(ToolMessage(content=output, tool_call_id=call["id"], name=call["name"]))
    else:
        # 轮数用尽仍在调工具：不带工具再调一次，强制给出结论
        messages.append(HumanMessage("请停止调用工具，基于已有执行结果直接给出结论。"))
        response = await llm.ainvoke(messages)

    content = response.content if isinstance(response.content, str) else str(response.content)
    result = StepResult(
        step_id=task.step_id, iteration=task.iteration, index=task.index,
        title=task.step.title, description=task.step.description, step_type=task.step.step_type,
        execution_res=content, references=[],
    )
    emit("coder_output", step_id=task.step_id, title=task.step.title, content=content)
    return {"step_results": {task.step_id: result}}
