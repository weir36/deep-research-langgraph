"""本地知识库（mock 时序图中的"知识库平台"和"临时知识库"）。

- 全局知识库：data/kb 下的 .md / .txt 文件；
- 临时知识库：本次任务用户上传的文件（State.resources）。
检索方式为分块 + 字符 bigram 相关度，生产环境替换为向量检索服务即可。
"""

from __future__ import annotations

# 可以先把它理解成一个简化版的 RAG（检索增强生成）系统。
# 不过，这段代码有一个特点：它没有使用 Embedding 向量检索，
# 也没有使用 Elasticsearch，
# 而是使用字符 bigram（字符二元组）计算文本相关度。

from functools import lru_cache
from pathlib import Path

from ..config import DATA_DIR
from ..references import chunk_text, relevance
from ..schemas import Reference

KB_DIR = DATA_DIR / "kb"
_SUFFIXES = {".md", ".txt"}


@lru_cache(maxsize=64)
def _load_chunks(path: str, mtime: float) -> list[str]:
    return chunk_text(Path(path).read_text(encoding="utf-8", errors="ignore"), size=500)


def _files(resources: list[str]) -> list[Path]:
    files = sorted(p for p in KB_DIR.rglob("*") if p.suffix in _SUFFIXES) if KB_DIR.exists() else []
    files += [Path(r) for r in resources if Path(r).is_file()]
    return files


def has_knowledge(resources: list[str]) -> bool:
    return bool(_files(resources))


def knowledge_search(query: str, resources: list[str], top_k: int = 4, min_score: float = 0.15) -> list[Reference]:
    scored: list[tuple[float, Path, str]] = []
    for path in _files(resources):
        for chunk in _load_chunks(str(path), path.stat().st_mtime):
            score = relevance(query, chunk)
            if score >= min_score:
                scored.append((score, path, chunk))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [
        Reference(kind="doc", title=path.name, file_id=path.name, content=chunk, score=round(score, 3))
        for score, path, chunk in scored[:top_k]
    ]
