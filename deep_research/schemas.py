"""核心数据结构：研究计划、引用、步骤执行结果。"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class StepType(str, Enum):
    RESEARCH = "research"  # 信息搜索步骤
    PROCESSING = "processing"  # 数据处理步骤


class Step(BaseModel):
    """单个研究步骤（只描述"做什么"，执行结果单独存放在 StepResult）。"""

    need_search: bool = Field(default=True, description="是否需要搜索")
    title: str = Field(..., description="步骤标题")
    description: str = Field(..., description="具体要搜索或处理什么")
    step_type: StepType = Field(default=StepType.RESEARCH, description="步骤类型")
    depends_on: list[int] = Field(
        default_factory=list,
        description="依赖的步骤序号（从 1 开始）。无依赖的步骤会并行执行",
    )

    @model_validator(mode="before")
    @classmethod
    def _lenient(cls, data: Any) -> Any:
        # 前端编辑计划时只能改标题和描述，新增步骤会缺 step_type / need_search。
        if not isinstance(data, dict):
            return data
        data = dict(data)
        step_type = str(data.get("step_type") or "").strip().lower()
        if step_type not in {"research", "processing"}:
            step_type = "processing" if data.get("need_search") is False else "research"
        data["step_type"] = step_type
        if data.get("need_search") is None:
            data["need_search"] = step_type == "research"
        if data.get("depends_on") is None:
            data["depends_on"] = []
        return data


class Plan(BaseModel):
    """完整的研究计划（Planner 的结构化输出）。"""

    locale: str = Field(default="zh-CN", description="语言设置，如 zh-CN")
    has_enough_context: bool = Field(default=False, description="现有信息是否已足够写报告")
    thought: str = Field(default="", description="规划思路")
    title: str = Field(..., description="研究主题")
    steps: list[Step] = Field(default_factory=list, description="研究步骤列表")


class Reference(BaseModel):
    """一条引用来源。index 不存在这里：步骤内用列表位置编号，报告阶段再统一重编号。"""

    kind: Literal["web", "doc"] = "web"
    title: str = ""
    url: str = ""
    file_id: str = ""
    content: str = ""  # 经过筛选的正文片段
    score: float = 0.0

    @property
    def key(self) -> str:
        return self.url or f"doc://{self.file_id}#{self.content[:50]}"

    @property
    def location(self) -> str:
        return self.url or (f"rag://{self.file_id}" if self.file_id else "")


class StepResult(BaseModel):
    """一个步骤的执行结果。execution_res 中的 [[n]] 指向本步骤 references 的第 n 条。"""

    step_id: str
    iteration: int
    index: int
    title: str
    description: str
    step_type: StepType
    execution_res: str
    references: list[Reference] = Field(default_factory=list)


class StepTask(BaseModel):
    """research_team 分发给 researcher / coder 的任务包（通过 Send 传递）。"""

    step_id: str
    iteration: int
    index: int
    step: Step
    research_topic: str
    locale: str = "zh-CN"
    context: list[StepResult] = Field(default_factory=list)  # 可参考的已完成步骤
    resources: list[str] = Field(default_factory=list)  # 用户上传的文件


def step_id(iteration: int, index: int) -> str:
    """iteration 从 1 开始，index 为步骤在计划中的 0-based 位置。"""
    return f"p{iteration}s{index + 1}"
