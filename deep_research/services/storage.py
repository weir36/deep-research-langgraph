"""资源存储服务 mock：最终报告按 thread_id 落盘，对应时序图中的"最终报告 / 获取最终报告"。"""

from __future__ import annotations

import json
from datetime import datetime

from ..config import RUNTIME_DIR

REPORT_DIR = RUNTIME_DIR / "reports"


def save_report(thread_id: str, topic: str, report: str, meta: dict | None = None) -> str:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"{thread_id}.md"
    path.write_text(report, encoding="utf-8")
    info = {"thread_id": thread_id, "topic": topic, "saved_at": datetime.now().isoformat(timespec="seconds"), **(meta or {})}
    (REPORT_DIR / f"{thread_id}.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


def load_report(thread_id: str) -> str | None:
    path = REPORT_DIR / f"{thread_id}.md"
    return path.read_text(encoding="utf-8") if path.exists() else None
