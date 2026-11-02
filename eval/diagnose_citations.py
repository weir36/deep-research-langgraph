"""引用准确率分层诊断：同一个核验器，分别核验「步骤结论」和「最终报告」，定位引用是在哪一层挂错的。

    python -m eval.diagnose_citations eval/results/<dir> [--variants optimized] [--sample 15]

只读已有结果目录里的 checkpoint，不发起任何搜索。
- 步骤层：researcher_output 写的结论（局部编号 [[n]] 对应本步骤的引用列表）
- 报告层：reporter 合并改写后的报告（全局编号）
两层差距大 → reporter 改写时把引用挂歪了；步骤层本身就低 → 问题在 researcher_output 或摘录。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
from pathlib import Path

from deep_research.config import Settings
from deep_research.runner import open_graph
from deep_research.schemas import Reference

from .judge import verify_citations


async def diagnose(out_dir: Path, variants: list[str], sample: int) -> None:
    settings = Settings()
    rows = []
    async with open_graph(out_dir / "checkpoints.sqlite") as graph:
        for variant in variants:
            for path in sorted((out_dir / variant).glob("q??.json")):
                record = json.loads(path.read_text(encoding="utf-8"))
                if not record.get("collected_refs"):
                    continue
                state = (await graph.aget_state({"configurable": {"thread_id": f"{variant}-{record['id']}"}})).values
                steps = [r for r in state.get("step_results", {}).values() if r.references]
                step_checks = await asyncio.gather(
                    *(verify_citations(r.execution_res, r.references, settings, sample=sample) for r in steps)
                )
                refs = [Reference(**r) for r in record["references"]]
                report_check = await verify_citations(record["report"], refs, settings, sample=sample * 2)
                checked = sum(c["checked"] for c in step_checks)
                supported = sum(c["supported"] for c in step_checks)
                row = {
                    "variant": variant,
                    "id": record["id"],
                    "step": supported / checked if checked else None,
                    "report": report_check["citation_precision"],
                }
                rows.append(row)
                print(f"{variant:<12}{record['id']}  步骤层 {row['step'] or 0:.2f}（{checked} 条）  报告层 {row['report'] or 0:.2f}", flush=True)

    for variant in variants:
        vs = [r for r in rows if r["variant"] == variant]
        if vs:
            step = statistics.mean(r["step"] for r in vs if r["step"] is not None)
            report = statistics.mean(r["report"] for r in vs if r["report"] is not None)
            print(f"\n[{variant}] 步骤层均值 {step:.2f}  报告层均值 {report:.2f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("out_dir", type=Path)
    ap.add_argument("--variants", default="optimized")
    ap.add_argument("--sample", type=int, default=5, help="每个步骤抽查的引用数")
    args = ap.parse_args()
    asyncio.run(diagnose(args.out_dir, args.variants.split(","), args.sample))


if __name__ == "__main__":
    main()
