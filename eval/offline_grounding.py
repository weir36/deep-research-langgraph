"""离线验证引用剪枝：拿已跑完的真实步骤结论做剪枝（basic 模型），再用评测核验器（reasoning 模型）复核。

    python -m eval.offline_grounding eval/results/<dir> [--variant optimized] [--sample 6]

不发起任何搜索，只消耗 LLM 调用。剪枝和核验是两个不同的模型，避免"自己出题自己改"。
注意：这里只能验证「事后剪枝」这一层，提示词约束要真实重跑才能看到效果。
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from deep_research.config import Settings
from deep_research.grounding import cited_sentences, prune_unsupported
from deep_research.runner import open_graph

from .judge import verify_citations


async def main_async(out_dir: Path, variant: str, sample: int) -> None:
    settings = Settings()
    async with open_graph(out_dir / "checkpoints.sqlite") as graph:
        steps = []
        for path in sorted((out_dir / variant).glob("q??.json")):
            qid = json.loads(path.read_text(encoding="utf-8"))["id"]
            state = (await graph.aget_state({"configurable": {"thread_id": f"{variant}-{qid}"}})).values
            steps += [(qid, r) for r in state.get("step_results", {}).values() if r.references]
    sem = asyncio.Semaphore(4)

    async def one(qid, r):
        async with sem:
            pruned, dropped = await prune_unsupported(r.execution_res, r.references, settings)
            # 同一个随机种子抽样：剪枝前后核验的是同一批「句子 × 引用」里还剩下的那些
            b, a = await asyncio.gather(
                verify_citations(r.execution_res, r.references, settings, sample=sample),
                verify_citations(pruned, r.references, settings, sample=sample),
            )
            n_before = sum(len(i) for _, i in cited_sentences(r.execution_res))
            print(f"{qid} {r.step_id}: 剪掉 {dropped}/{n_before}  准确率 {b['citation_precision']} → {a['citation_precision']}", flush=True)
            return b, a, dropped, n_before

    rows = [x for x in await asyncio.gather(*(one(q, r) for q, r in steps)) if x[0]["checked"] and x[1]["checked"]]
    sup_b = sum(b["supported"] for b, *_ in rows); chk_b = sum(b["checked"] for b, *_ in rows)
    sup_a = sum(a["supported"] for _, a, *_ in rows); chk_a = sum(a["checked"] for _, a, *_ in rows)
    dropped = sum(d for *_, d, _ in rows); total = sum(n for *_, n in rows)
    print(f"\n步骤 {len(rows)} 个  引用准确率（核验模型 {settings.model_for('citation_checker')}）"
          f" {sup_b / chk_b:.2f} → {sup_a / chk_a:.2f}  删除引用 {dropped}/{total}（{dropped / total:.0%}）")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("out_dir", type=Path)
    ap.add_argument("--variant", default="optimized")
    ap.add_argument("--sample", type=int, default=6)
    args = ap.parse_args()
    asyncio.run(main_async(args.out_dir, args.variant, args.sample))


if __name__ == "__main__":
    main()
