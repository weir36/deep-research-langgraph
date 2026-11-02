"""运行入口：SQLite 持久化 checkpoint，支持中断后按 thread_id 恢复。"""

from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable

from langchain_core.messages import HumanMessage
import aiosqlite
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from .config import RUNTIME_DIR
from .graph import build_graph
from .llm import UsageTracker

CHECKPOINT_DB = RUNTIME_DIR / "checkpoints.sqlite"

# 存档里会出现的自定义类型。显式登记后，LangGraph 读存档时不再警告，也不会反序列化别的类型
CHECKPOINT_TYPES = [
    ("deep_research.schemas", name) for name in ("Plan", "Step", "StepType", "Reference", "StepResult", "StepTask")
]

# 计划审核回调：收到待审核的计划，返回 "ACCEPT" / "EDIT PLAN {...}"
ReviewFn = Callable[[dict], Awaitable[str]]
EventFn = Callable[[dict], None]


@dataclass
class RunResult:
    thread_id: str
    final_report: str
    state: dict
    usage: dict
    elapsed: float
    events: list[dict] = field(default_factory=list)


@asynccontextmanager
async def open_graph(db_path: str | None = None) -> AsyncIterator[Any]:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(str(db_path or CHECKPOINT_DB)) as conn:
        saver = AsyncSqliteSaver(conn, serde=JsonPlusSerializer(allowed_msgpack_modules=CHECKPOINT_TYPES))
        yield build_graph(saver)


async def run(
    graph,
    *,
    query: str | None = None,
    thread_id: str | None = None,
    resources: list[str] | None = None,
    review: ReviewFn | None = None,
    on_event: EventFn | None = None,
    configurable: dict | None = None,
) -> RunResult:
    """新建任务（传 query）或恢复任务（传 thread_id，不传 query）。"""
    thread_id = thread_id or uuid.uuid4().hex[:12]
    tracker = UsageTracker()
    config = {
        "configurable": {"thread_id": thread_id, **(configurable or {})},
        "callbacks": [tracker],
        "recursion_limit": 100,
    }

    payload: Any
    if query is not None:
        payload = {"messages": [HumanMessage(query)], "resources": resources or [], "step_results": {}}
    else:
        snapshot = await graph.aget_state(config)
        if not snapshot.values:
            raise ValueError(f"找不到任务 {thread_id}")
        interrupts = [i for t in snapshot.tasks for i in t.interrupts]
        payload = Command(resume=await review(interrupts[0].value)) if interrupts and review else None

    events: list[dict] = []
    start = time.perf_counter()
    while True:
        # durability="sync"：每个超步的 checkpoint 写完才进入下一步。默认的 "async" 是边跑边写，
        # 进程被 kill 时最后一批写入可能还没落盘，恢复后已完成的搜索会重跑（见 tests/test_kill_resume.py）
        async for _ns, mode, chunk in graph.astream(
            payload, config, stream_mode=["custom", "updates"], subgraphs=True, durability="sync"
        ):
            if mode == "custom":
                events.append(chunk)
                if on_event:
                    on_event(chunk)
        snapshot = await graph.aget_state(config)
        interrupts = [i for t in snapshot.tasks for i in t.interrupts]
        if not interrupts:
            break
        if review is None:  # 无人审核：保持挂起，稍后可用 thread_id 恢复
            break
        payload = Command(resume=await review(interrupts[0].value))

    return RunResult(
        thread_id=thread_id,
        final_report=snapshot.values.get("final_report", ""),
        state=snapshot.values,
        usage=tracker.summary(),
        elapsed=time.perf_counter() - start,
        events=events,
    )


async def list_threads(graph, limit: int = 20) -> list[dict]:
    """列出未完成的任务（对应时序图中的"查看未结束任务"）。"""
    # 先收集 thread_id 再逐个查询：alist 迭代期间持有连接锁，在循环内调用 aget_state 会死锁
    thread_ids: list[str] = []
    async for cp in graph.checkpointer.alist(None, limit=1000):
        cfg = cp.config["configurable"]
        if not cfg.get("checkpoint_ns") and cfg["thread_id"] not in thread_ids:
            thread_ids.append(cfg["thread_id"])
    pending = []
    for tid in thread_ids:
        snapshot = await graph.aget_state({"configurable": {"thread_id": tid}})
        if snapshot.next:
            pending.append({"thread_id": tid, "topic": snapshot.values.get("research_topic", ""), "next": list(snapshot.next)})
            if len(pending) >= limit:
                break
    return pending
