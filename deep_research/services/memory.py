"""用户记忆服务 mock：每次研究结束后记录主题与摘要，对应时序图中的"记忆存储"。"""

from __future__ import annotations

import json
from datetime import datetime

from ..config import RUNTIME_DIR

MEMORY_FILE = RUNTIME_DIR / "memory.jsonl"


def remember(thread_id: str, topic: str, report: str) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    title = next((line.lstrip("# ").strip() for line in report.splitlines() if line.startswith("# ")), topic)
    record = {"thread_id": thread_id, "topic": topic, "title": title, "at": datetime.now().isoformat(timespec="seconds")}
    with MEMORY_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
