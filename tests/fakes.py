"""脚本化的假 LLM：根据 system prompt 判断当前是哪个 Agent，返回预设响应。"""

from __future__ import annotations

import itertools
import json
import re
from collections import Counter
from typing import Any, Callable

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

_ids = itertools.count()

DEFAULT_PLAN = {
    "locale": "zh-CN",
    "has_enough_context": False,
    "thought": "对比两座建筑的高度",
    "title": "世界最高建筑与埃菲尔铁塔高度对比",
    "steps": [
        {"need_search": True, "title": "步骤A 世界最高建筑", "description": "收集哈利法塔高度", "step_type": "research", "depends_on": []},
        {"need_search": True, "title": "步骤B 埃菲尔铁塔", "description": "收集埃菲尔铁塔高度", "step_type": "research", "depends_on": []},
        {"need_search": False, "title": "步骤C 计算倍数", "description": "计算高度倍数", "step_type": "processing", "depends_on": [1, 2]},
    ],
}


def _call(name: str, args: dict) -> dict:
    return {"name": name, "args": args, "id": f"call_{next(_ids)}", "type": "tool_call"}


def _agent(messages: list[BaseMessage]) -> str:
    system = next((m.content for m in messages if isinstance(m, SystemMessage)), "")
    for marker, name in [
        ("`researcher_output` agent", "researcher_output"),
        ("`researcher` agent", "researcher"),
        ("`coder` agent", "coder"),
        ("professional Deep Researcher", "planner"),
        ("distinguished academic researcher", "reporter"),
        ("领域智能体", "coordinator"),
        ("研究报告评审专家", "judge"),
        ("判断给定的来源内容是否支持", "citation_checker"),
        ("判断每个来源是否为这句论断提供了依据", "grounding"),
    ]:
        if marker in system:
            return name
    return "unknown"


def _step_title(messages: list[BaseMessage]) -> str:
    human = next(m.content for m in messages if isinstance(m, HumanMessage))
    return human.split("# Current Step\n## Title\n", 1)[-1].split("\n", 1)[0]


class FakeWorld:
    def __init__(self) -> None:
        self.calls: Counter[tuple[str, str]] = Counter()  # (agent, step_title) -> 次数
        self.plan = DEFAULT_PLAN
        self.followup_plan: dict | None = None  # 第二轮计划，None 表示信息已充足
        self.hooks: dict[str, Callable[[list[BaseMessage]], None]] = {}  # agent -> 调用前回调（可抛异常）
        self.model = ScriptedChatModel(world=self)

    def respond(self, messages: list[BaseMessage]) -> AIMessage:
        agent = _agent(messages)
        step = _step_title(messages) if agent in {"researcher", "researcher_output", "coder"} else ""
        self.calls[(agent, step)] += 1
        if agent in self.hooks:
            self.hooks[agent](messages)

        if agent == "coordinator":
            text = messages[-1].content
            if text in {"你好", "hi"}:
                return AIMessage("你好！我是一名专注于AI搜索的领域智能体。")
            return AIMessage("", tool_calls=[_call("handoff_to_planner", {"research_topic": text, "locale": "zh-CN"})])

        if agent == "planner":
            followup = "Findings From Previous Rounds" in messages[-1].content
            plan = self.plan if not followup else (self.followup_plan or {**self.plan, "has_enough_context": True, "steps": []})
            return AIMessage("", tool_calls=[_call("Plan", plan)])

        if agent == "researcher":
            if not any(isinstance(m, ToolMessage) for m in messages):
                return AIMessage("", tool_calls=[
                    _call("web_search", {"query": f"{step} 数据"}),
                    _call("web_search", {"query": f"{step} 官方"}),
                ])
            return AIMessage("research job done", tool_calls=[_call("handoff_to_researcher_output", {})])

        if agent == "researcher_output":
            return AIMessage(
                f"## 问题陈述\n{step}\n## 结论\n关键数据[[1]][[2]]，越界引用[[99]]。\n这是一句编造的推断[[3]]。"
            )

        if agent == "coder":
            if not any(isinstance(m, ToolMessage) for m in messages):
                return AIMessage("", tool_calls=[_call("python_repl_tool", {"code": "print(828/330)"})])
            out = next(m.content for m in messages if isinstance(m, ToolMessage))
            return AIMessage(f"计算结果：{out.split('Stdout:')[-1].strip()}")

        if agent == "reporter":
            return AIMessage("# 报告\n哈利法塔最高[[1]]，铁塔数据[[4]]，编造的引用[[50]]。")

        if agent == "judge":
            dim = {"reason": "测试", "score": 80}
            keys = ["factual_accuracy", "coverage_depth", "structure_fluency", "citation_quality", "risk_bias"]
            return AIMessage("", tool_calls=[_call("JudgeResult", {k: dim for k in keys})])

        if agent == "grounding":
            claim = messages[-1].content.split("# 论断\n", 1)[-1].split("\n", 1)[0]
            cited = [int(i) for i in re.findall(r"# 来源 \[(\d+)\]", messages[-1].content)]
            return AIMessage("", tool_calls=[_call("JointVerdict", {"supporting": [] if "编造" in claim else cited, "reason": "测试"})])

        if agent == "citation_checker":
            # 论断里带「编造」字样的视为来源不支持，用来测试 grounding 会删掉它的引用
            claim = messages[-1].content.split("# 论断\n", 1)[-1].split("\n", 1)[0]
            supported = "编造" not in claim
            return AIMessage("", tool_calls=[_call("CitationVerdict", {"supported": supported, "reason": "测试"})])

        raise AssertionError(f"unexpected agent for messages: {messages[:1]}")


class ScriptedChatModel(BaseChatModel):
    world: Any

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):  # 工具 schema 不影响脚本响应
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        msg = self.world.respond(messages)
        msg.usage_metadata = {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}
        return ChatResult(generations=[ChatGeneration(message=msg)], llm_output={"model_name": "gpt-4.1-mini"})


def edit_plan_payload(plan: dict) -> str:
    return "EDIT PLAN " + json.dumps(plan, ensure_ascii=False)
