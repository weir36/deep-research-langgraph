"""运行配置。

所有可调参数集中在 Settings 中：默认值来自环境变量，单次运行可通过
`config["configurable"]` 覆盖（评测脚本就是靠这一点对比不同变体）。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, Field

ROOT_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """读取项目根目录的 .env（不覆盖已存在的环境变量）。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if value[:1] not in {'"', "'"} and " #" in value:  # 去掉行尾注释（引号里的 # 保留）
            value = value.split(" #", 1)[0].rstrip()
        os.environ.setdefault(key.strip(), value.strip("\"'"))


_load_dotenv(ROOT_DIR / ".env")
DATA_DIR = Path(os.getenv("DR_DATA_DIR", ROOT_DIR / "data"))
RUNTIME_DIR = DATA_DIR / "runtime"

# 每百万 token 的美元价格（input, output）。价格会变动，以官方页面为准。
MODEL_PRICES: dict[str, tuple[float, float]] = {
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
}


def _env_prices(raw: str) -> dict[str, tuple[float, float]]:
    """DR_MODEL_PRICES="gemini-4-argon=1.25/10,gemini-3.5-flash=0.3/2.5"，单位同上（美元 / 百万 token）。"""
    prices = {}
    for item in filter(None, (x.strip() for x in raw.split(","))):
        name, _, value = item.partition("=")
        price_in, _, price_out = value.partition("/")
        try:
            prices[name.strip()] = (float(price_in), float(price_out))
        except ValueError:
            continue
    return prices


MODEL_PRICES.update(_env_prices(os.getenv("DR_MODEL_PRICES", "")))
USD_TO_CNY = float(os.getenv("DR_USD_TO_CNY", "7.1"))

Tier = Literal["basic", "reasoning"]

# 模型分级：调工具、整合单步结论用便宜模型；规划和写报告用强模型。
DEFAULT_NODE_TIERS: dict[str, Tier] = {
    "coordinator": "basic",
    "planner": "reasoning",
    "researcher": "basic",
    "researcher_output": "basic",
    "coder": "basic",
    "reporter": "reasoning",
    "judge": "reasoning",
    "grounding": "basic",  # 线上剪枝不支持的引用
    # 评测核验固定用强模型，和线上剪枝（basic）不是同一个模型，避免"自己出题自己改"
    "citation_checker": "reasoning",
}


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class Settings(BaseModel):
    # ---- 模型 ----
    basic_model: str = Field(default_factory=lambda: os.getenv("DR_BASIC_MODEL", "gpt-4.1-mini"))
    reasoning_model: str = Field(default_factory=lambda: os.getenv("DR_REASONING_MODEL", "gpt-4.1"))
    node_tiers: dict[str, Tier] = Field(default_factory=lambda: dict(DEFAULT_NODE_TIERS))

    # ---- 流程控制 ----
    locale: str = "zh-CN"
    max_plan_iterations: int = 2  # 最多生成几轮计划（首轮 + 补充轮）
    max_step_num: int = 3
    max_research_rounds: int = 5  # researcher 单步最多几轮工具调用
    max_coder_rounds: int = 4
    max_search_results: int = 5
    auto_accept_plan: bool = False

    # ---- 优化开关（评测时用来做对照组）----
    parallel_steps: bool = True  # 无依赖的步骤并发执行
    max_concurrency: int = 4
    context_strategy: Literal["truncate", "rerank"] = "rerank"  # 引用内容截断 vs 分块重排
    ref_char_budget: int = 1500  # rerank 模式下每条引用保留的字符数
    chunk_size: int = 500
    validate_citations: bool = True  # 删除越界引用编号
    # 逐句核验步骤结论的引用，删掉来源不支持的编号（见 grounding.py）。默认关：评测里它在提示词约束之上
    # 只再提升 0.03 的引用准确率（0.785 → 0.816，5 题内在噪声范围），却让 LLM 调用数 ×3.7、成本 +50%。
    # 对引用要求极严的场景（报告直接对外）再打开
    ground_citations: bool = False

    # ---- Coder 沙箱 ----
    coder_timeout: int = 30
    coder_allow_network: bool = Field(default_factory=lambda: _env_bool("DR_CODER_ALLOW_NETWORK", False))

    def model_for(self, node: str) -> str:
        tier = self.node_tiers.get(node, "basic")
        return self.reasoning_model if tier == "reasoning" else self.basic_model


def get_settings(config: RunnableConfig | None = None) -> Settings:
    configurable = (config or {}).get("configurable", {}) or {}
    overrides = {k: v for k, v in configurable.items() if k in Settings.model_fields}
    return Settings(**overrides)
