"""评测脚本：同一题集、同一打分模型下，对比不同配置变体。

    python -m eval.run_eval                                  # baseline vs optimized，全部题目
    python -m eval.run_eval --limit 5                        # 只跑前 5 题
    python -m eval.run_eval --variants optimized,no_rerank   # 指定变体（消融实验）
    python -m eval.run_eval --rejudge eval/results/<dir>     # 只重新打分，不重新生成报告

每个变体都是一组 configurable 覆盖项，baseline 关闭所有优化，便于把指标提升归因到具体改动。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from datetime import datetime
from pathlib import Path

from deep_research.config import DEFAULT_NODE_TIERS, Settings
from deep_research.references import cited_indices
from deep_research.runner import open_graph, run
from deep_research.schemas import Reference
from deep_research.tools.search import CachedBackend, get_search_backend, set_search_backend

from .judge import DIMENSIONS, judge_report, verify_citations

ROOT = Path(__file__).parent
ALL_REASONING = {k: "reasoning" for k in DEFAULT_NODE_TIERS}

VARIANTS: dict[str, dict] = {
    # 参考实现的做法：串行执行、正文截断 500 字、不校验引用、所有节点用同一个强模型
    "baseline": {"parallel_steps": False, "context_strategy": "truncate", "validate_citations": False,
                 "ground_citations": False, "node_tiers": ALL_REASONING},
    "optimized": {},  # 默认配置：并行 + 正文重排 + 越界清理 + 模型分档 + 引用提示词约束
    "strict": {"ground_citations": True},  # 在 optimized 之上再加逐句引用剪枝
    # 上一版 optimized：旧版 researcher_output 提示词（没有引用约束）。结果从上一轮复用，见 REUSE_ONLY
    "v1": {},
    "v1_no_parallel": {"parallel_steps": False},
    # 消融：每次只关掉一项优化
    "no_parallel": {"parallel_steps": False},
    "no_rerank": {"context_strategy": "truncate"},
    "no_validation": {"validate_citations": False},
    "no_tiering": {"node_tiers": ALL_REASONING},
}


# 只复用已有结果、不重新生成的变体：baseline 和 v1 系列是用上一版提示词生成的，现在重跑已经不是同一个东西
REUSE_ONLY = {"baseline", "v1", "v1_no_parallel"}
# 一次研究大约要 33 次搜索；剩余预算不够一整次就不开始，免得跑到一半额度耗尽、白花前面的搜索
SEARCHES_PER_RUN = 40
_IN_FLIGHT = [0]  # 正在生成的任务数


def load_questions(limit: int | None, ids: list[str] | None) -> list[dict]:
    qs = [json.loads(line) for line in (ROOT / "questions.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    if ids:
        qs = [q for q in qs if q["id"] in ids]
    return qs[:limit] if limit else qs


async def generate(graph, variant: str, question: dict, out_dir: Path, sem: asyncio.Semaphore, common: dict) -> dict:
    path = out_dir / variant / f"{question['id']}.json"
    if path.exists():  # 断点续跑：已生成的报告不重复生成
        return json.loads(path.read_text(encoding="utf-8"))
    if variant in REUSE_ONLY and not common.get("_allow_regenerate"):
        return {"id": question["id"], "variant": variant, "error": "复用变体缺少上一轮的结果文件"}
    async with sem:
        backend = get_search_backend()
        # 预算 = 已用 + 在跑的每个任务各预留一份。只看已用会让并发的几个任务一起超支，跑到一半额度耗尽
        if isinstance(backend, CachedBackend) and backend.budget is not None \
                and backend.budget - backend.misses - SEARCHES_PER_RUN * (_IN_FLIGHT[0] + 1) < 0:
            print(f"⏭ [{variant}] {question['id']} 搜索预算不足，不开始", flush=True)
            return {"id": question["id"], "variant": variant, "error": "搜索预算不足，未开始"}
        _IN_FLIGHT[0] += 1
        configurable = {"auto_accept_plan": True, **{k: v for k, v in common.items() if not k.startswith("_")},
                        **VARIANTS[variant]}
        print(f"▶ [{variant}] {question['id']} {question['query'][:30]}…", flush=True)
        try:
            res = await run(graph, query=question["query"], thread_id=f"{variant}-{question['id']}", configurable=configurable)
        except Exception as exc:
            print(f"✗ [{variant}] {question['id']} 失败：{exc}", flush=True)
            return {"id": question["id"], "variant": variant, "error": str(exc)}
        finally:
            _IN_FLIGHT[0] -= 1
    refs = res.state.get("report_references", [])
    record = {
        "id": question["id"],
        "variant": variant,
        "query": question["query"],
        "report": res.final_report,
        "references": [r.model_dump() for r in refs],
        "elapsed_s": round(res.elapsed, 1),
        "cost_usd": round(res.usage["total"]["cost_usd"], 5),
        "llm_calls": res.usage["total"]["calls"],
        "usage_by_node": res.usage["by_node"],
        "collected_refs": len(refs),
        "cited_refs": len([i for i in cited_indices(res.final_report) if 1 <= i <= len(refs)]),
        "plan_iterations": res.state.get("plan_iterations", 0),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / variant / f"{question['id']}.md").write_text(res.final_report, encoding="utf-8")
    print(f"✓ [{variant}] {question['id']} {record['elapsed_s']}s ${record['cost_usd']}", flush=True)
    return record


async def evaluate(record: dict, settings: Settings, sem: asyncio.Semaphore, out_dir: Path, force: bool) -> dict:
    path = out_dir / record["variant"] / f"{record['id']}.eval.json"
    if path.exists() and not force:
        return {**record, **json.loads(path.read_text(encoding="utf-8"))}
    refs = [Reference(**r) for r in record["references"]]
    async with sem:
        # 网关偶发 502：打分失败重试，不能让一次抖动丢掉整轮评测（报告已经生成、搜索额度已经花了）
        for attempt in range(4):
            try:
                judged, citations = await asyncio.gather(
                    judge_report(record["query"], record["report"], refs, settings),
                    verify_citations(record["report"], refs, settings),
                )
                break
            except Exception as exc:  # noqa: BLE001
                if attempt == 3:
                    print(f"✗ [{record['variant']}] {record['id']} 打分失败：{exc}", flush=True)
                    return {**record, "error": f"打分失败：{exc}"}
                await asyncio.sleep(5 * (attempt + 1))
    scores = judged.scores()
    result = {
        "scores": scores,
        "overall": round(sum(scores.values()) / len(scores), 2),
        "reasons": {k: getattr(judged, k).reason for k in DIMENSIONS},
        "citations": citations,
    }
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"⚖ [{record['variant']}] {record['id']} 总分 {result['overall']} 引用准确率 {citations['citation_precision']}", flush=True)
    return {**record, **result}


def _mean(values: list[float]) -> str:
    values = [v for v in values if v is not None]
    if not values:
        return "-"
    if len(values) == 1:
        return f"{values[0]:.2f}"
    return f"{statistics.mean(values):.2f} ± {statistics.stdev(values):.2f}"


def summarize(rows: list[dict], variants: list[str], judge_model: str) -> str:
    by_variant = {v: [r for r in rows if r["variant"] == v and "scores" in r] for v in variants}
    metrics = [
        ("综合得分", lambda r: r["overall"]),
        *[(label, (lambda k: lambda r: r["scores"][k])(key)) for key, label in DIMENSIONS.items()],
        ("引用准确率", lambda r: r["citations"]["citation_precision"]),
        ("越界引用数", lambda r: r["citations"]["invalid_citations"]),
        ("无引用的数字断言", lambda r: r["citations"]["uncited_numeric_claims"]),
        ("收集来源数", lambda r: r["collected_refs"]),
        ("报告引用来源数", lambda r: r["cited_refs"]),
        ("耗时 (s)", lambda r: r["elapsed_s"]),
        ("成本 (USD)", lambda r: r["cost_usd"]),
        ("LLM 调用次数", lambda r: r["llm_calls"]),
    ]
    lines = [
        f"# 评测结果（{datetime.now():%Y-%m-%d %H:%M}）",
        "",
        f"- 打分模型：`{judge_model}`；题目数：{max((len(v) for v in by_variant.values()), default=0)}",
        "- 数值为 均值 ± 标准差；成本只含 LLM 推理，不含搜索 API。",
        "",
        "| 指标 | " + " | ".join(variants) + " |",
        "|---|" + "---:|" * len(variants),
    ]
    for name, fn in metrics:
        lines.append(f"| {name} | " + " | ".join(_mean([fn(r) for r in by_variant[v]]) for v in variants) + " |")

    # 逐题对比：只统计两个变体都成功的题目，避免样本不同造成偏差
    if len(variants) >= 2:
        a, b = variants[0], variants[1]
        pa = {r["id"]: r for r in by_variant[a]}
        pb = {r["id"]: r for r in by_variant[b]}
        common = sorted(set(pa) & set(pb))
        if common:
            wins = sum(pb[i]["overall"] > pa[i]["overall"] for i in common)
            ties = sum(pb[i]["overall"] == pa[i]["overall"] for i in common)
            lines += [
                "",
                f"## 逐题对比：{b} vs {a}（{len(common)} 题）",
                "",
                f"- {b} 胜 {wins} 题，平 {ties} 题，负 {len(common) - wins - ties} 题",
                "",
                f"| 题目 | {a} | {b} | 差值 |",
                "|---|---:|---:|---:|",
            ]
            for i in common:
                lines.append(f"| {i} | {pa[i]['overall']:.1f} | {pb[i]['overall']:.1f} | {pb[i]['overall'] - pa[i]['overall']:+.1f} |")
    failed = [f"{r['variant']}/{r['id']}" for r in rows if "error" in r]
    if failed:
        lines += ["", f"失败：{', '.join(failed)}"]
    return "\n".join(lines) + "\n"


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--variants", default="baseline,optimized")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--ids", help="逗号分隔的题目 ID")
    parser.add_argument("--concurrency", type=int, default=2, help="同时生成的报告数")
    parser.add_argument("--judge-model", default=None, help="打分模型，默认使用 reasoning 模型")
    parser.add_argument("--out", help="结果目录，默认 eval/results/<时间戳>；指定已有目录可断点续跑")
    parser.add_argument("--rejudge", action="store_true", help="忽略已有打分结果，重新打分")
    parser.add_argument("--max-steps", type=int, default=3)
    parser.add_argument("--max-iterations", type=int, default=2)
    parser.add_argument("--allow-regenerate", action="store_true",
                        help="允许重新生成 REUSE_ONLY 变体（测试用；正式评测里它们应复用上一轮结果）")
    parser.add_argument("--search-budget", type=int, default=None,
                        help="本次最多发出的真实搜索次数（缓存命中不计）。Tavily advanced 每次 2 credit")
    args = parser.parse_args()

    variants = args.variants.split(",")
    unknown = [v for v in variants if v not in VARIANTS]
    if unknown:
        parser.error(f"未知变体：{unknown}，可选：{list(VARIANTS)}")
    questions = load_questions(args.limit, args.ids.split(",") if args.ids else None)
    out_dir = Path(args.out) if args.out else ROOT / "results" / datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)

    # 搜索结果落盘缓存：同一个查询跨变体只搜一次；断点续跑时也不会重复消耗额度
    set_search_backend(CachedBackend(get_search_backend(), out_dir / "search_cache.jsonl", args.search_budget))
    common = {"max_step_num": args.max_steps, "max_plan_iterations": args.max_iterations,
              "_allow_regenerate": args.allow_regenerate}
    judge_settings = Settings(**({"reasoning_model": args.judge_model} if args.judge_model else {}))
    start = time.perf_counter()

    gen_sem = asyncio.Semaphore(args.concurrency)
    # 所有任务共用一个 graph（一个 SQLite 连接），避免多连接并发写入导致 database is locked
    async with open_graph(out_dir / "checkpoints.sqlite") as graph:
        records = await asyncio.gather(*(generate(graph, v, q, out_dir, gen_sem, common) for v in variants for q in questions))
    judge_sem = asyncio.Semaphore(4)
    rows = await asyncio.gather(*(
        evaluate(r, judge_settings, judge_sem, out_dir, args.rejudge) if "error" not in r else asyncio.sleep(0, r)
        for r in records
    ))

    summary = summarize(list(rows), variants, judge_settings.model_for("judge"))
    (out_dir / "summary.md").write_text(summary, encoding="utf-8")
    print("\n" + summary)
    backend = get_search_backend()
    print(f"结果目录：{out_dir}  总耗时 {time.perf_counter() - start:.0f}s  "
          f"搜索：真实 {backend.misses} 次 / 缓存命中 {backend.hits} 次")


if __name__ == "__main__":
    asyncio.run(main())
