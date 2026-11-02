import sys

from eval import run_eval
from eval.judge import extract_claims


def test_extract_claims():
    report = """# 标题
哈利法塔高828米[[1]][[2]]。上海中心高632米。
| 建筑 | 高度 |
|---|---|
- 这一句没有数字也没有引用，因此不计入统计。

## 参考文献
[1] 不应被计入 2024[[3]]。
"""
    cited, uncited = extract_claims(report)
    assert cited == [("哈利法塔高828米。", [1, 2])]
    assert uncited == 1


def test_extract_claims_joins_wrapped_lines():
    report = """**存储**：SK海力士占据近6成市场份额，主
导供应链利润[[31]]。
- 第一项列表[[1]]。
- 第二项列表[[2]]。
"""
    cited, _ = extract_claims(report)
    assert cited == [
        ("存储**：SK海力士占据近6成市场份额，主导供应链利润。", [31]),  # 行首的 * 被 strip 掉，不影响核验
        ("第一项列表。", [1]),
        ("第二项列表。", [2]),
    ]


async def test_eval_pipeline_end_to_end(world, tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["run_eval", "--limit", "2", "--out", str(tmp_path), "--allow-regenerate"])
    await run_eval.main()
    summary = (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert "| 综合得分 | 80.00 ± 0.00 | 80.00 ± 0.00 |" in summary
    assert "optimized 胜 0 题，平 2 题" in summary
    # baseline 不校验引用，越界编号 [[50]] 会被核验脚本统计出来
    assert "| 越界引用数 | 1.00 ± 0.00 | 0.00 ± 0.00 |" in summary
