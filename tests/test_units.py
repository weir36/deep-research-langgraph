from deep_research.nodes.human_feedback import parse_feedback
from deep_research.nodes.research_team import ready_steps
from deep_research.references import (
    build_global_references, chunk_text, remap_citations, render_reference_list, select_excerpt, validate_citations,
)
from deep_research.schemas import Plan, Reference, StepResult, StepType, step_id
from deep_research.tools.sandbox import run_python


def _plan(*steps):
    return Plan(title="t", steps=list(steps))


def test_validate_and_remap_citations():
    text, dropped = validate_citations("a[[1]][[3]] b[[0]] c[[2]][[2]]", num_refs=2)
    assert text == "a[[1]] b c[[2]]" and dropped == 2
    assert remap_citations("x[[1]][[2]] y[[3]]", {1: 5, 2: 5}) == "x[[5]] y"


def test_global_references_dedupe_by_url():
    shared = Reference(url="https://a", title="A")
    r1 = StepResult(step_id="p1s1", iteration=1, index=0, title="s1", description="", step_type=StepType.RESEARCH,
                    execution_res="甲[[1]]乙[[2]]", references=[shared, Reference(url="https://b")])
    r2 = StepResult(step_id="p1s2", iteration=1, index=1, title="s2", description="", step_type=StepType.RESEARCH,
                    execution_res="丙[[1]]丁[[2]]", references=[Reference(url="https://c"), shared])
    refs, rewritten = build_global_references([r1, r2])
    assert [r.url for r in refs] == ["https://a", "https://b", "https://c"]
    assert rewritten["p1s2"] == "丙[[3]]丁[[1]]"
    listing = render_reference_list("正文[[3]][[1]][[9]]", refs)
    assert "[1] [A](https://a)" in listing and "[3]" in listing and "[9]" not in listing


def test_rerank_keeps_relevant_tail_that_truncate_loses():
    raw = "\n".join(["无关的导航栏和广告内容。" * 10] * 5 + ["哈利法塔的建筑高度为828米，共162层。"])
    query = "哈利法塔 高度"
    assert "828" not in select_excerpt(query, "", raw, strategy="truncate")
    assert "828" in select_excerpt(query, "", raw, strategy="rerank", budget=300, chunk_size=120)


def test_chunk_text_respects_size():
    chunks = chunk_text("长" * 1200, size=500, overlap=80)
    assert all(len(c) <= 500 for c in chunks) and len(chunks) == 3


def test_lenient_plan_edit():
    plan = _plan()
    edited = parse_feedback('EDIT PLAN {"steps": [{"title": "a", "description": "b"}, '
                            '{"title": "c", "description": "d", "need_search": false}]}', plan)
    assert edited.steps[0].step_type == StepType.RESEARCH and edited.steps[0].need_search
    assert edited.steps[1].step_type == StepType.PROCESSING
    assert parse_feedback("ACCEPT", plan) is plan


def test_ready_steps_dependencies():
    plan = _plan(
        {"title": "r1", "description": ""},
        {"title": "r2", "description": ""},
        {"title": "p", "description": "", "step_type": "processing"},  # 未声明依赖 -> 依赖之前所有步骤
        {"title": "r3", "description": "", "depends_on": [9, 4]},  # 非法依赖被忽略
    )
    assert ready_steps(plan, 1, set()) == [0, 1, 3]
    assert ready_steps(plan, 1, {step_id(1, 0)}) == [1, 3]
    assert ready_steps(plan, 1, {step_id(1, i) for i in (0, 1, 3)}) == [2]


def test_ready_steps_cycle_falls_back_to_sequential():
    plan = _plan({"title": "a", "description": "", "depends_on": [2]}, {"title": "b", "description": "", "depends_on": [1]})
    assert ready_steps(plan, 1, set()) == [0]


async def test_sandbox_isolation():
    assert "2.0" in await run_python("import numpy as np\nprint(np.mean([1, 3]))")
    assert "disabled" in await run_python("import urllib.request\nurllib.request.urlopen('http://example.com')")
    assert "超时" in await run_python("while True: pass", timeout=2)
    assert "Stdout: None" in await run_python("import os\nprint(os.environ.get('OPENAI_API_KEY'))")


async def test_prune_unsupported_keeps_layout(world):
    from deep_research.config import Settings
    from deep_research.grounding import prune_unsupported
    from deep_research.schemas import Reference

    text = "## 结论\n\n- 份额约六成，主\n导供应链利润[[1]][[2]]。\n- 这是编造的结论[[2]]。\n\n结尾段落[[1]]。"
    refs = [Reference(title="a", url="u1", content="x"), Reference(title="b", url="u2", content="y")]
    out, dropped = await prune_unsupported(text, refs, Settings())
    assert dropped == 1
    # 没改动的段落原样保留（包括句中换行），只改动有不支持引用的那一段
    assert out == "## 结论\n\n- 份额约六成，主\n导供应链利润[[1]][[2]]。\n- 这是编造的结论。\n\n结尾段落[[1]]。"


async def test_cached_backend_budget(tmp_path):
    import pytest

    from deep_research.tools.search import CachedBackend, MockBackend, SearchBudgetExhausted

    cache = CachedBackend(MockBackend(), tmp_path / "c.jsonl", budget=1)
    first = await cache.search("固态电池 量产", 3)
    assert await cache.search("  固态电池   量产 ", 3) == first  # 空白和大小写归一后命中缓存
    with pytest.raises(SearchBudgetExhausted):
        await cache.search("另一个查询", 3)
    reloaded = CachedBackend(MockBackend(), tmp_path / "c.jsonl", budget=0)
    assert await reloaded.search("固态电池 量产", 3) == first  # 缓存落盘，预算为 0 也能命中
    assert (cache.misses, cache.hits) == (1, 1)


async def test_cached_backend_survives_unicode_line_separator(tmp_path):
    from deep_research.tools.search import CachedBackend, SearchHit

    class OneHit:
        async def search(self, query, max_results):
            return [SearchHit(title="t", url="u", snippet="s", raw_content="第一段\u2028第二段")]

    path = tmp_path / "c.jsonl"
    await CachedBackend(OneHit(), path).search("q", 1)
    reloaded = CachedBackend(OneHit(), path, budget=0)  # 读回缓存不能被 \u2028 切断
    assert (await reloaded.search("q", 1))[0].raw_content == "第一段\u2028第二段"
