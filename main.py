"""命令行入口。

    python main.py "研究问题"                    # 新建研究，交互式审核计划
    python main.py "研究问题" --auto-accept      # 自动接受计划
    python main.py "研究问题" -f 报告.md          # 附带本地文件（临时知识库）
    python main.py --list                       # 查看未结束的任务
    python main.py --resume <thread_id>         # 从断点继续
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from deep_research.runner import list_threads, open_graph, run
from deep_research.services.storage import load_report


def print_plan(plan: dict) -> None:
    print(f"\n📋 研究计划：{plan['title']}\n   思路：{plan['thought']}")
    for i, s in enumerate(plan["steps"], 1):
        deps = f"（依赖步骤 {s['depends_on']}）" if s.get("depends_on") else ""
        print(f"   {i}. [{s['step_type']}] {s['title']}{deps}\n      {s['description']}")


async def interactive_review(payload: dict) -> str:
    plan = payload["plan"]
    edited = False
    print_plan(plan)
    while True:
        answer = (await asyncio.to_thread(input, "\n回车确认；d<编号> 删除步骤；t 修改标题：")).strip()
        if not answer:
            break
        if answer.startswith("d") and answer[1:].isdigit() and 0 < int(answer[1:]) <= len(plan["steps"]):
            plan["steps"].pop(int(answer[1:]) - 1)
            edited = True
        elif answer == "t":
            plan["title"] = (await asyncio.to_thread(input, "新标题：")).strip() or plan["title"]
            edited = True
        print_plan(plan)
    return "EDIT PLAN " + json.dumps(plan, ensure_ascii=False) if edited else "ACCEPT"


def make_printer(verbose: bool):
    def on_event(e: dict) -> None:
        kind = e["event"]
        if kind == "research_team":
            print(f"\n🔀 并行分发步骤：{', '.join(e['steps'])}")
        elif kind == "tool_act":
            arg = e["args"].get("query") or (e["args"].get("code", "")[:60] + "…")
            print(f"   🔧 [{e['step_id']}] {e['tool']}: {arg}")
        elif kind == "researcher_output":
            print(f"   ✅ [{e['step_id']}] {e['title']}（{e['references']} 条引用）")
            if verbose:
                print(e["content"])
        elif kind == "coder_output":
            print(f"   ✅ [{e['step_id']}] {e['title']}")
        elif kind == "planner" and e.get("action") == "max_iterations":
            print("\n📝 达到最大计划轮数，开始撰写报告")
        elif kind == "planner" and e["plan"]["has_enough_context"]:
            print("\n📝 信息充足，开始撰写报告")
        elif kind == "planner" and e["plan_iterations"] > 1:
            print(f"\n🔁 第 {e['plan_iterations']} 轮补充研究")
            print_plan(e["plan"])

    return on_event


async def main() -> None:
    parser = argparse.ArgumentParser(description="Deep Research 多 Agent 研究助手")
    parser.add_argument("query", nargs="?")
    parser.add_argument("-f", "--file", action="append", default=[], help="附带的本地文件，可重复")
    parser.add_argument("--auto-accept", action="store_true")
    parser.add_argument("--resume", metavar="THREAD_ID")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--max-steps", type=int, default=3)
    parser.add_argument("--max-iterations", type=int, default=2)
    parser.add_argument("--sequential", action="store_true", help="关闭步骤并行（对照用）")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)

    async with open_graph() as graph:
        if args.list:
            for t in await list_threads(graph):
                print(f"{t['thread_id']}  下一步={t['next']}  {t['topic']}")
            return
        if not args.query and not args.resume:
            parser.print_help()
            sys.exit(1)
        if args.resume and (report := load_report(args.resume)):
            print(report)
            return

        result = await run(
            graph,
            query=args.query if not args.resume else None,
            thread_id=args.resume,
            resources=args.file,
            review=None if args.auto_accept else interactive_review,
            on_event=make_printer(args.verbose),
            configurable={
                "auto_accept_plan": args.auto_accept,
                "max_step_num": args.max_steps,
                "max_plan_iterations": args.max_iterations,
                "parallel_steps": not args.sequential,
            },
        )
    print("\n" + "=" * 80 + "\n")
    print(result.final_report)
    print("\n" + "=" * 80)
    t = result.usage["total"]
    print(f"任务 ID：{result.thread_id}  耗时：{result.elapsed:.1f}s  "
          f"LLM 调用：{t['calls']} 次  token：{t['input_tokens']} 入 / {t['output_tokens']} 出  "
          f"成本：${t['cost_usd']:.4f}（约 ¥{t['cost_cny']:.2f}）")
    if result.usage["unpriced_models"]:
        print(f"注意：{', '.join(result.usage['unpriced_models'])} 没有价格，成本按 0 计。"
              "在 .env 里加 DR_MODEL_PRICES=模型名=输入价/输出价（美元 / 百万 token）。")


if __name__ == "__main__":
    asyncio.run(main())
