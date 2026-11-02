"""报告质量评测。

两类指标：
1. 五维度 LLM 打分（0-100）：事实准确性、覆盖度与深度、结构与行文、引用质量、风险与偏见控制。
   每个维度有明确的分档标准，要求先写扣分理由再给分，降低打分模型"一律给高分"的倾向。
2. 引用核验（不依赖打分模型的主观印象）：抽取报告中带 [[n]] 的句子，逐条判断第 n 个来源
   是否支持该句子，得到引用准确率；无任何引用的事实性句子计入"无据断言"。
"""

from __future__ import annotations

import asyncio
import random
import re

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from deep_research.config import Settings
from deep_research.grounding import SENT_RE, CitationVerdict, judge_support, paragraphs
from deep_research.llm import get_llm
from deep_research.references import CITE_RE, strip_citations
from deep_research.schemas import Reference

DIMENSIONS = {
    "factual_accuracy": "事实准确性（幻觉率）",
    "coverage_depth": "覆盖度与深度",
    "structure_fluency": "行文流畅与结构清晰",
    "citation_quality": "引用质量与溯源性",
    "risk_bias": "风险与偏见控制",
}

RUBRIC = """你是一名严格的研究报告评审专家。请根据用户的研究问题、报告正文和报告所依据的来源材料，从五个维度独立打分（0-100 的整数）。

# 评分维度与分档
1. factual_accuracy 事实准确性：报告中的数据、事实是否与来源材料一致，有无编造或曲解。
   90+ 无可见错误；75-89 有个别细节偏差；60-74 有明显错误或无来源的数据；<60 多处编造。
2. coverage_depth 覆盖度与深度：是否回答了问题的所有子问题，是否有具体数据、多个视角和深入分析。
   90+ 全部覆盖且有深度；75-89 覆盖主要方面但部分浅显；60-74 遗漏重要子问题；<60 大量缺失。
3. structure_fluency 行文与结构：结构是否清晰、逻辑是否连贯、表格使用是否恰当、有无冗余重复。
4. citation_quality 引用质量：关键论断是否都有引用，引用是否指向合适的来源，来源是否多样可信。
   90+ 关键论断均有恰当引用；75-89 少量论断缺引用；60-74 引用稀疏或错配；<60 基本无引用。
5. risk_bias 风险与偏见控制：是否呈现多方观点、是否说明数据局限性和不确定性、是否避免绝对化和偏颇结论。

# 要求
- 先针对每个维度写出具体的扣分理由（引用报告中的具体内容），再给分。
- 不要因为报告篇幅长或语言华丽就给高分。
- 各维度独立评分，不要互相影响。"""


class DimensionScore(BaseModel):
    reason: str = Field(description="扣分理由，需引用报告中的具体内容")
    score: int = Field(ge=0, le=100)


class JudgeResult(BaseModel):
    factual_accuracy: DimensionScore
    coverage_depth: DimensionScore
    structure_fluency: DimensionScore
    citation_quality: DimensionScore
    risk_bias: DimensionScore

    def scores(self) -> dict[str, int]:
        return {k: getattr(self, k).score for k in DIMENSIONS}


def _sources_digest(refs: list[Reference], per_ref: int = 300, max_chars: int = 12000) -> str:
    parts, used = [], 0
    for i, r in enumerate(refs, 1):
        piece = f"[{i}] {r.title}\n{r.content[:per_ref]}\n"
        if used + len(piece) > max_chars:
            parts.append(f"……（其余 {len(refs) - i + 1} 条来源省略）")
            break
        parts.append(piece)
        used += len(piece)
    return "\n".join(parts) or "（无来源）"


def _body(report: str) -> str:
    """去掉系统追加的参考文献列表，只评正文。"""
    return report.split("\n## 参考文献", 1)[0]


async def judge_report(query: str, report: str, refs: list[Reference], settings: Settings) -> JudgeResult:
    llm = get_llm("judge", settings).with_structured_output(JudgeResult, method="function_calling")
    content = (
        f"# 研究问题\n{query}\n\n# 来源材料（节选）\n{_sources_digest(refs)}\n\n# 待评审报告\n{_body(report)}"
    )
    return await llm.ainvoke([SystemMessage(RUBRIC), HumanMessage(content)])


# ---------------------------------------------------------------------------
# 引用核验
# ---------------------------------------------------------------------------



def extract_claims(report: str) -> tuple[list[tuple[str, list[int]]], int]:
    """返回 (带引用的句子及其编号, 无引用的事实性句子数)。"""
    cited, uncited = [], 0
    for line in paragraphs(_body(report)):
        for sent in SENT_RE.findall(line):
            idx = [int(i) for i in CITE_RE.findall(sent)]
            text = strip_citations(sent).strip(" -*|")
            if len(text) < 6:
                continue
            if idx:
                cited.append((text, idx))
            elif re.search(r"\d", text):  # 含数字但无引用：视为无据的事实断言
                uncited += 1
    return cited, uncited


async def verify_citations(
    report: str, refs: list[Reference], settings: Settings, *, sample: int = 25, seed: int = 0,
) -> dict:
    claims, uncited = extract_claims(report)
    pairs = [(text, i) for text, idx in claims for i in idx if 1 <= i <= len(refs)]
    invalid = sum(1 for _, idx in claims for i in idx if not 1 <= i <= len(refs))
    random.Random(seed).shuffle(pairs)
    pairs = pairs[:sample]

    llm = get_llm("citation_checker", settings).with_structured_output(CitationVerdict, method="function_calling")
    sem = asyncio.Semaphore(8)

    async def check(text: str, i: int) -> bool | None:
        async with sem:  # 超时 / 解析失败返回 None，不计入分母
            return await judge_support(llm, text, refs[i - 1])

    results = [r for r in await asyncio.gather(*(check(t, i) for t, i in pairs)) if r is not None]
    checked = len(results)
    return {
        "cited_claims": len(claims),
        "uncited_numeric_claims": uncited,
        "invalid_citations": invalid,
        "checked": checked,
        "supported": sum(results),
        "citation_precision": round(sum(results) / checked, 4) if checked else None,
    }
