"""LLM 工厂与 token / 成本统计。"""

from __future__ import annotations

import os
import threading
from collections import defaultdict
from typing import Any, Callable
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.outputs import LLMResult

from .config import MODEL_PRICES, USD_TO_CNY, Settings

_factory_override: Callable[[str, Settings], BaseChatModel] | None = None
_cache: dict[str, BaseChatModel] = {}


def set_llm_factory(factory: Callable[[str, Settings], BaseChatModel] | None) -> None:
    """测试用：替换真实模型。"""
    global _factory_override
    _factory_override = factory
    _cache.clear()


def get_llm(node: str, settings: Settings) -> BaseChatModel:
    if _factory_override is not None:
        return _factory_override(node, settings)
    model = settings.model_for(node)
    if model not in _cache:
        from langchain_openai import ChatOpenAI

        _cache[model] = ChatOpenAI(
            model=model,
            temperature=0,
            base_url=os.getenv("OPENAI_BASE_URL") or None,
            stream_usage=True,
            max_retries=3,
            timeout=180,
        )
    return _cache[model]


def _price(model: str) -> tuple[float, float] | None:
    # 带日期后缀的模型名（如 gpt-4.1-mini-2025-04-14）按最长前缀匹配；查不到返回 None
    for name in sorted(MODEL_PRICES, key=len, reverse=True):
        if model.startswith(name):
            return MODEL_PRICES[name]
    return None


class UsageTracker(BaseCallbackHandler):
    """按 LangGraph 节点汇总 LLM 调用次数、token 与成本。

    通过 config["callbacks"] 传入，LangChain 会把它传播到图中所有的模型调用。
    """

    run_inline = True  # 在调用线程同步执行，避免异步场景下统计丢失

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs: dict[UUID, tuple[str, str]] = {}
        self.unpriced_models: set[str] = set()  # 价格表里查不到的模型，成本按 0 计
        self.by_node: dict[str, dict[str, float]] = defaultdict(
            lambda: {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
        )

    def on_chat_model_start(self, serialized, messages, *, run_id: UUID, metadata=None, invocation_params=None, **kwargs: Any) -> None:
        node = (metadata or {}).get("langgraph_node", "unknown")
        params = invocation_params or kwargs.get("invocation_params") or {}
        model = params.get("model") or params.get("model_name") or ""
        with self._lock:
            self._runs[run_id] = (node, model)

    def on_llm_end(self, response: LLMResult, *, run_id: UUID, **kwargs: Any) -> None:
        with self._lock:
            node, model = self._runs.pop(run_id, ("unknown", ""))
        input_tokens = output_tokens = 0
        for generations in response.generations:
            for gen in generations:
                usage = getattr(getattr(gen, "message", None), "usage_metadata", None) or {}
                input_tokens += usage.get("input_tokens", 0)
                output_tokens += usage.get("output_tokens", 0)
        model = (response.llm_output or {}).get("model_name") or model
        price = _price(model)
        price_in, price_out = price or (0.0, 0.0)
        with self._lock:
            if price is None:
                self.unpriced_models.add(model or "unknown")
            rec = self.by_node[node]
            rec["calls"] += 1
            rec["input_tokens"] += input_tokens
            rec["output_tokens"] += output_tokens
            rec["cost_usd"] += (input_tokens * price_in + output_tokens * price_out) / 1_000_000

    def summary(self) -> dict[str, Any]:
        total = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
        for rec in self.by_node.values():
            for k in total:
                total[k] += rec[k]
        total["cost_cny"] = round(total["cost_usd"] * USD_TO_CNY, 4)
        return {
            "total": total,
            "by_node": {k: dict(v) for k, v in self.by_node.items()},
            "unpriced_models": sorted(self.unpriced_models),
        }

    def format_table(self) -> str:
        s = self.summary()
        lines = ["| 节点 | 调用次数 | 输入 token | 输出 token | 成本(USD) |", "|---|---:|---:|---:|---:|"]
        for node, rec in sorted(s["by_node"].items(), key=lambda kv: -kv[1]["cost_usd"]):
            lines.append(f"| {node} | {rec['calls']} | {rec['input_tokens']} | {rec['output_tokens']} | {rec['cost_usd']:.4f} |")
        t = s["total"]
        lines.append(f"| **合计** | {t['calls']} | {t['input_tokens']} | {t['output_tokens']} | {t['cost_usd']:.4f}（约 ¥{t['cost_cny']:.2f}） |")
        if s["unpriced_models"]:
            lines.append(f"\n注意：{', '.join(s['unpriced_models'])} 没有价格，成本按 0 计。在 .env 的 DR_MODEL_PRICES 里补上。")
        return "\n".join(lines)
