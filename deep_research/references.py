"""引用治理：正文片段筛选、引用编号校验、跨步骤全局重编号。

设计要点：
- 每个步骤内部用局部编号 [[1..k]]，步骤之间互不干扰，因此可以并行执行；
- 报告阶段按步骤顺序合并引用，按 URL 去重，生成全局编号并改写各步骤的结论；
- 参考文献列表由代码生成，LLM 不写 URL，避免编造链接。
"""

from __future__ import annotations

import re
from typing import Iterable

from .schemas import Reference, StepResult

CITE_RE = re.compile(r"\[\[(\d+)\]\]")
_TOKEN_RE = re.compile(r"[a-z0-9]+|[一-鿿]")


# ---------------------------------------------------------------------------
# 片段筛选
# ---------------------------------------------------------------------------

def _features(text: str) -> set[str]:
    """中文按字 bigram、英文按单词切分。无需分词器，中英混排也能用。"""
    tokens = _TOKEN_RE.findall(text.lower())
    feats = {t for t in tokens if len(t) > 1}
    cjk = [t for t in tokens if len(t) == 1 and "一" <= t <= "鿿"]
    feats.update(a + b for a, b in zip(cjk, cjk[1:]))
    return feats


def relevance(query: str, text: str) -> float:
    q = _features(query)
    if not q:
        return 0.0
    return len(q & _features(text)) / len(q)


def chunk_text(text: str, size: int = 500, overlap: int = 80) -> list[str]:
    """按段落切块，超长段落再按固定长度切。"""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n|\n", text) if p.strip()]
    chunks: list[str] = []
    buf = ""
    for p in paragraphs:
        while len(p) > size:
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.append(p[:size])
            p = p[size - overlap:]
        if len(buf) + len(p) + 1 > size and buf:
            chunks.append(buf)
            buf = ""
        buf = f"{buf}\n{p}" if buf else p
    if buf:
        chunks.append(buf)
    return chunks


def select_excerpt(
    query: str,
    snippet: str,
    raw_content: str | None,
    *,
    strategy: str,
    budget: int = 1500,
    chunk_size: int = 500,
) -> str:
    """从网页正文中挑出与 query 最相关的片段。

    - truncate：参考实现的做法，取前 500 字。正文后半部分的数据会丢失。
    - rerank：正文切块后按相关度排序，在字符预算内选出最相关的块，再按原文顺序拼接。
    """
    if strategy == "truncate":
        return (raw_content or snippet)[:500]
    if not raw_content:
        return snippet[:budget]
    chunks = chunk_text(raw_content, size=chunk_size)
    ranked = sorted(range(len(chunks)), key=lambda i: relevance(query, chunks[i]), reverse=True)
    picked: list[int] = []
    used = len(snippet)
    for i in ranked:
        if used + len(chunks[i]) > budget:
            continue
        picked.append(i)
        used += len(chunks[i])
    parts = [snippet] if snippet else []
    parts += [chunks[i] for i in sorted(picked) if chunks[i] not in snippet]
    return "\n...\n".join(parts)[: budget + len(snippet)]


# ---------------------------------------------------------------------------
# 引用编号
# ---------------------------------------------------------------------------

def _dedupe_adjacent(text: str) -> str:
    """[[2]][[2]] -> [[2]]（重编号后同一来源可能相邻重复）。"""
    return re.sub(r"(\[\[(\d+)\]\])(?:\s*\[\[\2\]\])+", r"\1", text)


def validate_citations(text: str, num_refs: int) -> tuple[str, int]:
    """删除越界编号，返回 (清理后的文本, 删除个数)。"""
    dropped = 0

    def repl(m: re.Match) -> str:
        nonlocal dropped
        if 1 <= int(m.group(1)) <= num_refs:
            return m.group(0)
        dropped += 1
        return ""

    return _dedupe_adjacent(CITE_RE.sub(repl, text)), dropped


def remap_citations(text: str, mapping: dict[int, int]) -> str:
    """局部编号 -> 全局编号；映射表之外的编号直接删除。"""

    def repl(m: re.Match) -> str:
        new = mapping.get(int(m.group(1)))
        return f"[[{new}]]" if new else ""

    return _dedupe_adjacent(CITE_RE.sub(repl, text))


def strip_citations(text: str) -> str:
    return CITE_RE.sub("", text)


def cited_indices(text: str) -> list[int]:
    return sorted({int(i) for i in CITE_RE.findall(text)})


def build_global_references(results: Iterable[StepResult]) -> tuple[list[Reference], dict[str, str]]:
    """合并所有步骤的引用并去重，返回 (全局引用列表, step_id -> 重编号后的结论)。"""
    global_refs: list[Reference] = []
    key_to_index: dict[str, int] = {}
    rewritten: dict[str, str] = {}
    for result in results:
        mapping: dict[int, int] = {}
        for local_idx, ref in enumerate(result.references, start=1):
            if ref.key not in key_to_index:
                global_refs.append(ref)
                key_to_index[ref.key] = len(global_refs)
            mapping[local_idx] = key_to_index[ref.key]
        rewritten[result.step_id] = remap_citations(result.execution_res, mapping)
    return global_refs, rewritten


def render_reference_list(report: str, refs: list[Reference]) -> str:
    """只列出报告中实际引用到的来源，保持编号不变。"""
    used = [i for i in cited_indices(report) if 1 <= i <= len(refs)]
    if not used:
        return ""
    lines = ["", "---", "", "## 参考文献", ""]
    for i in used:
        ref = refs[i - 1]
        title = ref.title or ref.location or f"来源 {i}"
        lines.append(f"[{i}] [{title}]({ref.location})" if ref.location else f"[{i}] {title}")
        lines.append("")
    return "\n".join(lines)


def format_references_for_prompt(refs: list[Reference], *, with_content: bool = True) -> str:
    parts = []
    for i, ref in enumerate(refs, start=1):
        if with_content:
            parts.append(f"## Reference [{i}]: {ref.title}\n<reference>\n{ref.content}\n</reference>\n")
        else:
            parts.append(f"[{i}] {ref.title} ({ref.location})")
    return "\n".join(parts)
