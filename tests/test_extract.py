"""extract 黄金场景测试:干净命中 / 歧义搁置 / 失败 / 引用归属(BUILD_SPEC §5 三情形)。"""

import pytest

from aivis.contracts.config import FrozenConfig
from aivis.contracts.records import Citation
from aivis.pipeline.extract import canonical_domain, extract, normalize_url, owner_of_domain
from tests.conftest import make_response

# ---------------------------------------------------------------- URL 纯函数


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("HTTPS://WWW.Osprey.com/packs/", "https://www.osprey.com/packs"),
        ("https://osprey.com:443/packs", "https://osprey.com/packs"),
        ("https://osprey.com/packs?utm_source=x&b=2&a=1", "https://osprey.com/packs?a=1&b=2"),
        ("https://osprey.com/packs#section", "https://osprey.com/packs"),
        ("https://osprey.com/", "https://osprey.com"),
        ("not a url", "not a url"),
    ],
)
def test_normalize_url(raw: str, expected: str) -> None:
    assert normalize_url(raw) == expected


def test_canonical_domain_strips_www_and_port() -> None:
    assert canonical_domain("https://www.osprey.com:443/x") == "osprey.com"
    assert canonical_domain("https://shop.osprey.com/x") == "shop.osprey.com"


def test_owner_of_domain_suffix_match(demo_cfg: FrozenConfig) -> None:
    assert (o := owner_of_domain("alphapacks.example", demo_cfg.peers)) and o.name == "Alpha Packs"
    sub = owner_of_domain("shop.alphapacks.example", demo_cfg.peers)
    assert sub is not None and sub.name == "Alpha Packs"
    assert owner_of_domain("unrelated.example", demo_cfg.peers) is None
    # 防假域名投毒:notalphapacks.example 不是 alphapacks.example 的子域
    assert owner_of_domain("notalphapacks.example", demo_cfg.peers) is None


# ---------------------------------------------------------------- 提及:保守计数


def test_clean_mention_is_counted(demo_cfg: FrozenConfig) -> None:
    resp = make_response(answer_text="Alpha Packs is a solid choice. Alpha Packs fits well.")
    result = extract([resp], demo_cfg)
    m = next(x for x in result.mentions if x.brand == "Alpha Packs")
    assert m.mentioned and m.counted
    assert m.mention_count == 2
    assert m.first_position == 0
    assert m.reason is None


def test_ambiguous_alias_is_recorded_not_counted(demo_cfg: FrozenConfig) -> None:
    """缩写命中:mentioned=True 但 counted=False,进 extra data(CLAUDE.md §7)。"""
    resp = make_response(answer_text="Many hikers like AP for its straps.")
    result = extract([resp], demo_cfg)
    m = next(x for x in result.mentions if x.brand == "Alpha Packs")
    assert m.mentioned and not m.counted
    assert m.reason == "ambiguous_alias:AP"


def test_substring_does_not_match(demo_cfg: FrozenConfig) -> None:
    """词边界:papers/MAPS 里的 ap 不命中 AP;BravoX 不命中 Bravo。"""
    resp = make_response(answer_text="Check the papers and MAPS. BravoX is unrelated.")
    result = extract([resp], demo_cfg)
    assert all(not m.mentioned for m in result.mentions)


def test_absence_is_a_counted_observation(demo_cfg: FrozenConfig) -> None:
    resp = make_response(answer_text="No brands mentioned here.")
    result = extract([resp], demo_cfg)
    m = next(x for x in result.mentions if x.brand == "Bravo Gear")
    assert not m.mentioned and m.counted and m.mention_count == 0


def test_case_insensitive_match(demo_cfg: FrozenConfig) -> None:
    resp = make_response(answer_text="ALPHA PACKS and alpha packs both count.")
    result = extract([resp], demo_cfg)
    m = next(x for x in result.mentions if x.brand == "Alpha Packs")
    assert m.counted and m.mention_count == 2


# ---------------------------------------------------------------- 失败处理


def test_failed_response_recorded_not_extracted(demo_cfg: FrozenConfig) -> None:
    """工具错误显式记为失败,不冒充空回答(CLAUDE.md §9)。"""
    ok = make_response(question_id="q1", answer_text="Alpha Packs!")
    failed = make_response(
        question_id="q2", answer_text="", finished_ok=False, error_code="tool_error"
    )
    result = extract([ok, failed], demo_cfg)
    refs = {(r.question_id): r for r in result.response_refs}
    assert refs["q2"].finished_ok is False and refs["q2"].error_code == "tool_error"
    assert all(m.question_id != "q2" for m in result.mentions)


def test_mixed_run_ids_rejected(demo_cfg: FrozenConfig) -> None:
    with pytest.raises(ValueError, match="run"):
        extract([make_response(run_id="r1"), make_response(run_id="r2", run_index=1)], demo_cfg)


# ---------------------------------------------------------------- 引用归属


def _cite(
    url: str, title: str | None = None, cited_text: str | None = None, pos: int = 0
) -> Citation:
    return Citation(url=url, title=title, cited_text=cited_text, position=pos)


def test_owned_attribution_by_domain(demo_cfg: FrozenConfig) -> None:
    resp = make_response(
        answer_text="Alpha Packs is great.",
        citations=(_cite("https://www.alphapacks.example/products?utm_source=ai", "Shop"),),
    )
    result = extract([resp], demo_cfg)
    c = result.citations[0]
    assert c.brand == "Alpha Packs" and c.owned_or_earned == "owned"
    assert c.basis == "domain" and c.counted
    # 规范化保留 www(URL 须仍可抓取);跨 www 变体的归并靠 canonical_domain
    assert c.normalized_url == "https://www.alphapacks.example/products"


def test_earned_attribution_by_cited_metadata(demo_cfg: FrozenConfig) -> None:
    """已拍板口径 B:第三方页面 + 引用元数据含品牌清晰名 → earned。"""
    resp = make_response(
        answer_text="Alpha Packs is great.",
        citations=(
            _cite(
                "https://reviews.example/best-packs",
                title="Best packs of 2026: Alpha Packs tops the list",
            ),
        ),
    )
    result = extract([resp], demo_cfg)
    c = next(x for x in result.citations if x.brand == "Alpha Packs")
    assert c.owned_or_earned == "earned" and c.basis == "cited_metadata" and c.counted


def test_ambiguous_metadata_recorded_not_counted(demo_cfg: FrozenConfig) -> None:
    resp = make_response(
        answer_text="x",
        citations=(_cite("https://forum.example/t/1", title="Why AP straps win"),),
    )
    result = extract([resp], demo_cfg)
    c = next(x for x in result.citations if x.brand == "Alpha Packs")
    assert not c.counted and c.reason == "ambiguous_alias:AP"


def test_unattributed_citation_recorded(demo_cfg: FrozenConfig) -> None:
    resp = make_response(
        answer_text="x", citations=(_cite("https://news.example/article", title="Gear roundup"),)
    )
    result = extract([resp], demo_cfg)
    c = result.citations[0]
    assert c.brand is None and not c.counted and c.reason == "unattributed"


def test_owned_domain_blocks_earned_for_others(demo_cfg: FrozenConfig) -> None:
    """竞品官网标题点到别家名字,不给别家 earned(保守)。"""
    resp = make_response(
        answer_text="x",
        citations=(_cite("https://alphapacks.example/compare", title="Alpha Packs vs Bravo Gear"),),
    )
    result = extract([resp], demo_cfg)
    assert len(result.citations) == 1
    assert result.citations[0].brand == "Alpha Packs"


def test_extract_is_deterministic(demo_cfg: FrozenConfig) -> None:
    """同输入两次抽取逐字段相同(纯函数回归保险丝)。"""
    responses = [
        make_response(
            question_id=q,
            run_index=i,
            answer_text=f"Alpha Packs run {i} of {q}",
            citations=(_cite("https://alphapacks.example/p"),),
        )
        for q in ("q1", "q2")
        for i in (0, 1)
    ]
    assert extract(responses, demo_cfg) == extract(responses, demo_cfg)
