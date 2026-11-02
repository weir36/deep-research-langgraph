"""引用落地校验：researcher_output 写完结论后，逐条检查「句子 × 引用」，删掉来源不支持的编号。

为什么需要：分层诊断（eval/diagnose_citations.py）显示，引用准确率低的主因不在 reporter 改写，
而在步骤结论本身 —— 模型做"综合分析"时把多个来源的信息和自己的推断揉进一句话，句尾挂一串编号，
单看任何一个来源都撑不起这句话。提示词约束能减少这种写法，但只有逐条核验能保证结果。

成本：只用 basic 档模型，每句一次调用（一句和它引用的全部来源一起判断），不发起任何搜索；
步骤之间本来就并行，不额外增加耗时链路。判定失败（超时、解析失败）时保留原引用：宁可漏删，不误删。
"""

from __future__ import annotations

import asyncio
import logging
import re

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from .config import Settings
from .llm import get_llm
from .references import CITE_RE, _dedupe_adjacent
from .schemas import Reference

logger = logging.getLogger(__name__)

# 句子 + 句尾紧跟的引用。句末标点之后的 [[n]] 也算这句的
SENT_RE = re.compile(r"[^。！？!?\n]+[。！？!?]?(?:\s*\[\[\d+\]\])*")
_SKIP = ("#", "|", "---", ">", "![", "```")
_ITEM_RE = re.compile(r"^(?:[-*+]|\d+[.)])\s")

CHECK_PROMPT = """判断给定的来源内容是否支持报告中的论断。
- 论断中的核心事实、数字在来源中能找到依据：supported=true；
- 来源没有提及、或数字/结论与来源矛盾：supported=false。
只依据给出的来源内容判断，不要使用你自己的知识。"""


class CitationVerdict(BaseModel):
    supported: bool = Field(description="来源是否支持该论断（部分支持也算支持，但数字不一致算不支持）")
    reason: str = Field(description="一句话理由")


def paragraphs(body: str) -> list[str]:
    """把被硬换行折断的句子拼回去。模型常在句中换行（"……占据近6成市场份额，主\\n导供应链利润[[31]]"），
    逐行切句只能拿到后半句，核验时来源当然"不支持"。空行、标题、表格、列表项都是段落边界。"""
    paras: list[str] = []
    joinable = False
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line.startswith(_SKIP):
            joinable = False
            continue
        if joinable and not _ITEM_RE.match(line):
            paras[-1] += line
        else:
            paras.append(line)
        joinable = True
    return paras


def cited_sentences(text: str) -> list[tuple[str, list[int]]]:
    """返回 (去掉引用后的句子, 引用编号)。"""
    out = []
    for para in paragraphs(text):
        for sent in SENT_RE.findall(para):
            idx = [int(i) for i in CITE_RE.findall(sent)]
            claim = CITE_RE.sub("", sent).strip(" -*|")
            if idx and len(claim) >= 6:
                out.append((claim, idx))
    return out


async def judge_support(llm, claim: str, ref: Reference, timeout: float = 90) -> bool | None:
    """来源是否支持论断。调用失败返回 None，由调用方决定怎么算。"""
    try:
        verdict = await asyncio.wait_for(llm.ainvoke([
            SystemMessage(CHECK_PROMPT),
            HumanMessage(f"# 论断\n{claim}\n\n# 来源 {ref.title}\n{ref.content[:2000]}"),
        ]), timeout=timeout)
    except Exception:  # noqa: BLE001 - 超时 / 解析失败
        return None
    return verdict.supported


JOINT_PROMPT = """下面是研究报告中的一句论断，以及它引用的几个来源。判断每个来源是否为这句论断提供了依据。
- 一个来源只支撑了论断的一部分（比如区间的一端、几项数据中的一项），也算提供了依据；
- 来源没有提及论断里的事实，或数字与来源矛盾，才算没有提供依据。
只依据给出的来源内容判断，不要使用你自己的知识。"""


class JointVerdict(BaseModel):
    supporting: list[int] = Field(description="为论断提供了依据的来源编号；都没有则为空列表")
    reason: str = Field(description="一句话理由")


async def judge_sentence(llm, claim: str, cited: list[tuple[int, Reference]], timeout: float = 90) -> set[int] | None:
    """一句论断和它引用的全部来源一起判断，返回提供了依据的编号。失败返回 None。

    逐个来源单独判断会误伤"多个来源合起来才完整"的句子（"规模约 954 亿至 962 亿美元[[1]][[12]]"，
    两个来源各给一个数）：单看哪个来源都撑不起整句，两个引用都会被删掉。"""
    sources = "\n\n".join(f"# 来源 [{i}] {ref.title}\n{ref.content[:1500]}" for i, ref in cited)
    try:
        verdict = await asyncio.wait_for(llm.ainvoke([
            SystemMessage(JOINT_PROMPT), HumanMessage(f"# 论断\n{claim}\n\n{sources}"),
        ]), timeout=timeout)
    except Exception:  # noqa: BLE001 - 超时 / 解析失败
        return None
    return set(verdict.supporting)


async def prune_unsupported(text: str, refs: list[Reference], settings: Settings, *, max_checks: int = 20) -> tuple[str, int]:
    """删除来源不支持的引用编号，返回 (清理后的文本, 删除个数)。

    只删编号、不删句子：句子本身可能是合理的归纳，交给读者判断；但不能让它挂着一个撑不住它的来源。
    每句一次调用（不是每个引用一次），max_checks 限制单步核验的句子数，超出部分保持原样。"""
    sentences: dict[str, list[int]] = {}
    for claim, idx in cited_sentences(text):
        valid = [i for i in dict.fromkeys(idx) if 1 <= i <= len(refs)]
        if valid:
            sentences.setdefault(claim, valid)
    if not sentences:
        return text, 0
    llm = get_llm("grounding", settings).with_structured_output(JointVerdict, method="function_calling")
    sem = asyncio.Semaphore(6)

    async def check(claim: str, idx: list[int]):
        async with sem:
            return await judge_sentence(llm, claim, [(i, refs[i - 1]) for i in idx])

    items = list(sentences.items())[:max_checks]
    verdicts = await asyncio.gather(*(check(c, idx) for c, idx in items))
    unsupported = {(claim, i) for (claim, idx), ok in zip(items, verdicts) if ok is not None for i in idx if i not in ok}
    if not unsupported:
        return text, 0

    dropped = 0
    result = text
    for para in paragraphs(text):
        def fix(m: re.Match) -> str:
            sent = m.group(0)
            claim = CITE_RE.sub("", sent).strip(" -*|")

            def repl(c: re.Match) -> str:
                nonlocal dropped
                if (claim, int(c.group(1))) in unsupported:
                    dropped += 1
                    return ""
                return c.group(0)

            return CITE_RE.sub(repl, sent)

        fixed = SENT_RE.sub(fix, para)
        if fixed != para:  # 只替换改动过的段落，其余保持原文排版
            result = _replace_wrapped(result, para, fixed)
    return _dedupe_adjacent(result), dropped


def _replace_wrapped(text: str, before: str, after: str) -> str:
    """paragraphs() 拼接过的段落在原文里可能跨行。按行定位后整体替换成单行。"""
    lines = text.splitlines()
    for start in range(len(lines)):
        joined = ""
        for end in range(start, len(lines)):
            joined += lines[end].strip()
            if joined == before:
                indent = lines[start][: len(lines[start]) - len(lines[start].lstrip())]
                return "\n".join(lines[:start] + [indent + after] + lines[end + 1:])
            if not before.startswith(joined):
                break
    return text
