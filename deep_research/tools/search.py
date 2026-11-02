"""网页搜索。后端选择（DR_SEARCH_BACKEND）：
- auto（默认）：配置了 TAVILY_API_KEY 走 Tavily，否则走 DuckDuckGo（免费、不需要 key）；
- tavily / ddg / mock：强制指定。mock 只用于测试和离线跑通流程。

时序图中生产环境用的是字节火山搜索接口；实现 SearchBackend 协议即可替换。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class SearchHit(BaseModel):
    title: str
    url: str
    snippet: str
    raw_content: str | None = None
    score: float = 0.0


class SearchBudgetExhausted(RuntimeError):
    """搜索额度用完。和普通的搜索失败不同，这个错误要让整次研究失败，而不是交给 LLM "换个方式重试"——
    额度用完后每次搜索都失败，研究会在 0 个来源上照样写出一份报告，评测时被当成有效结果打分。"""


class SearchBackend(Protocol):
    async def search(self, query: str, max_results: int) -> list[SearchHit]: ...


class TavilyBackend:
    def __init__(self, api_key: str) -> None:
        from tavily import AsyncTavilyClient

        self._client = AsyncTavilyClient(api_key=api_key)

    async def search(self, query: str, max_results: int) -> list[SearchHit]:
        from tavily.errors import UsageLimitExceededError

        try:
            resp = await self._client.search(
                query,
                max_results=max_results,
                search_depth="advanced",
                include_raw_content="text",
            )
        except UsageLimitExceededError as exc:
            raise SearchBudgetExhausted(str(exc)) from exc
        return [
            SearchHit(
                title=r.get("title", ""),
                url=r.get("url", ""),
                snippet=r.get("content", ""),
                raw_content=r.get("raw_content"),
                score=float(r.get("score") or 0.0),
            )
            for r in resp.get("results", [])
        ]


class DuckDuckGoBackend:
    """免费搜索：DuckDuckGo 拿结果列表，再并发抓网页正文（抓不到就只用摘要）。"""

    def __init__(self, region: str = "cn-zh", fetch_timeout: float = 10.0, min_interval: float = 1.5, retries: int = 2) -> None:
        self.region = region
        self.fetch_timeout = fetch_timeout
        # 免费接口按 IP 限流，并行步骤一起搜很容易被封：所有查询排队，两次之间至少隔 min_interval 秒
        self.min_interval = min_interval
        self.retries = retries
        self._lock = asyncio.Lock()
        self._last = 0.0
        for name in ("trafilatura", "urllib3", "primp"):  # 抓网页失败是常态，不刷屏
            logging.getLogger(name).setLevel(logging.CRITICAL)

    @staticmethod
    def _fetch_text(url: str) -> str | None:
        import trafilatura

        html = trafilatura.fetch_url(url)
        return trafilatura.extract(html) if html else None

    async def _raw_content(self, url: str) -> str | None:
        try:
            return await asyncio.wait_for(asyncio.to_thread(self._fetch_text, url), self.fetch_timeout)
        except Exception as exc:  # 超时、反爬、PDF 等：退回摘要
            logger.info("fetch %s failed: %s", url, exc)
            return None

    async def _query(self, query: str, max_results: int) -> list[dict]:
        from ddgs import DDGS

        for attempt in range(self.retries + 1):
            async with self._lock:
                loop = asyncio.get_running_loop()
                wait = self._last + self.min_interval * (attempt + 1) - loop.time()
                if wait > 0:
                    await asyncio.sleep(wait)
                try:
                    return await asyncio.to_thread(lambda: DDGS().text(query, max_results=max_results, region=self.region))
                except Exception as exc:  # 没结果和被限流都是这个异常，分不清，统一退避重试
                    logger.info("ddg search %r failed (attempt %d): %s", query, attempt + 1, exc)
                finally:
                    self._last = loop.time()
        logger.warning("ddg search gave up: %r（可能被限流，配 TAVILY_API_KEY 更稳）", query)
        return []

    async def search(self, query: str, max_results: int) -> list[SearchHit]:
        rows = await self._query(query, max_results)
        contents = await asyncio.gather(*(self._raw_content(r.get("href", "")) for r in rows))
        return [
            SearchHit(
                title=r.get("title", ""),
                url=r.get("href", ""),
                snippet=r.get("body", ""),
                raw_content=content,
                score=1.0 - i * 0.05,  # DuckDuckGo 不给相关度，按排名递减
            )
            for i, (r, content) in enumerate(zip(rows, contents))
        ]


class MockBackend:
    """确定性的假结果：同一 query 返回相同内容，便于测试和离线演示。"""

    async def search(self, query: str, max_results: int) -> list[SearchHit]:
        hits = []
        for i in range(min(max_results, 3)):
            h = hashlib.md5(f"{query}-{i}".encode()).hexdigest()[:8]
            hits.append(
                SearchHit(
                    title=f"[MOCK] 关于「{query}」的资料 {i + 1}",
                    url=f"https://mock.search/{h}",
                    snippet=f"这是关于「{query}」的模拟搜索摘要 {i + 1}。",
                    raw_content=f"这是关于「{query}」的模拟网页正文 {i + 1}。\n当前是 mock 搜索，结果不含真实信息。",
                    score=1.0 - i * 0.1,
                )
            )
        return hits


class CachedBackend:
    """磁盘缓存 + 次数上限。同一个查询（不分大小写、首尾空格）只真正搜一次，跨研究、跨评测变体共享。

    budget 是允许发出的真实搜索次数（缓存命中不计），用完抛 SearchBudgetExhausted。"""

    def __init__(self, inner: SearchBackend, path: str | os.PathLike, budget: int | None = None) -> None:
        self.inner, self.path, self.budget = inner, Path(path), budget
        self.hits = self.misses = 0
        self._cache: dict[str, list[dict]] = {}
        if self.path.exists():
            # 只按 \n 切：splitlines() 还会在 \u2028 等 Unicode 行分隔符处切开，网页正文里会出现这些字符，
            # json.dumps(ensure_ascii=False) 又不转义它们，一行 JSON 会被切成两半
            for line in self.path.read_text(encoding="utf-8").split("\n"):
                if not line:
                    continue
                row = json.loads(line)
                self._cache[row["key"]] = row["hits"]
        self._lock = asyncio.Lock()

    @staticmethod
    def _key(query: str, max_results: int) -> str:
        return f"{max_results}|{' '.join(query.lower().split())}"

    async def search(self, query: str, max_results: int) -> list[SearchHit]:
        key = self._key(query, max_results)
        if key in self._cache:
            self.hits += 1
            return [SearchHit(**h) for h in self._cache[key]]
        async with self._lock:  # 并发下也不能超预算
            if self.budget is not None and self.misses >= self.budget:
                raise SearchBudgetExhausted(f"搜索预算 {self.budget} 次已用完")
            self.misses += 1
        hits = await self.inner.search(query, max_results)
        self._cache[key] = [h.model_dump() for h in hits]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"key": key, "hits": self._cache[key]}, ensure_ascii=False) + "\n")
        return hits


_backend: SearchBackend | None = None


def set_search_backend(backend: SearchBackend | None) -> None:
    global _backend
    _backend = backend


def get_search_backend() -> SearchBackend:
    global _backend
    if _backend is None:
        choice = os.getenv("DR_SEARCH_BACKEND", "auto").strip().lower()
        key = os.getenv("TAVILY_API_KEY")
        if choice == "mock":
            _backend = MockBackend()
        elif choice == "ddg" or (choice == "auto" and not key):
            _backend = DuckDuckGoBackend()
        else:
            _backend = TavilyBackend(key or "")
    return _backend
