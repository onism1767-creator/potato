"""verify 最小版测试:来源分类、离线口径诚实入档、缓存复用。"""

from aivis.contracts.checker import CheckOutcome
from aivis.contracts.config import FrozenConfig
from aivis.contracts.records import Citation, EvidenceRecord, ExtractionResult
from aivis.pipeline.extract import extract
from aivis.pipeline.verify import CHECK_OFFLINE, OfflineChecker, verify
from tests.conftest import FIXED_NOW, make_response


class _DownChecker:
    """全部不可达的核验器替身(演示:换实现只需新增类,verify 零改动)。"""

    @property
    def method(self) -> str:
        return "http-head"

    def check(self, url: str) -> CheckOutcome:
        return CheckOutcome(reachable=False, method=self.method, snippet=None)


def _extraction(demo_cfg: FrozenConfig) -> ExtractionResult:
    resp = make_response(
        answer_text="Alpha Packs is great.",
        citations=(
            Citation(url="https://www.alphapacks.example/p?utm_source=x", title="Shop", position=0),
            Citation(url="https://reviews.example/best", title="Alpha Packs review", position=1),
            Citation(url="https://news.example/a", title="Roundup", position=2),
        ),
    )
    return extract([resp], demo_cfg)


def test_verify_classifies_and_records_method(demo_cfg: FrozenConfig) -> None:
    extraction = _extraction(demo_cfg)
    records = verify(extraction, demo_cfg, checker=OfflineChecker(), now=FIXED_NOW)
    by_domain = {r.canonical_domain: r for r in records}
    assert len(records) == 3
    owned = by_domain["alphapacks.example"]
    assert owned.owned_or_earned == "owned" and owned.owner_brand == "Alpha Packs"
    assert owned.source_class == "brand_site"
    earned = by_domain["reviews.example"]
    assert earned.owned_or_earned == "earned" and earned.owner_brand is None
    assert all(r.check_method == CHECK_OFFLINE for r in records), "离线假设必须诚实入档"
    assert all(r.snippet is None for r in records), "v1 不抓页面正文"


def test_verify_reuses_fresh_cache(demo_cfg: FrozenConfig) -> None:
    """缓存里同方式、未过期的 URL 不再核验(限速友好/省钱)。"""
    extraction = _extraction(demo_cfg)
    first = verify(extraction, demo_cfg, checker=OfflineChecker(), now=FIXED_NOW)
    cache = {r.normalized_url: r for r in first}
    second = verify(
        extraction,
        demo_cfg,
        checker=OfflineChecker(),
        now="2026-06-12T00:00:00Z",
        cache=cache,
    )
    assert second == []


def test_refresh_ignores_cache_and_rechecks(demo_cfg: FrozenConfig) -> None:
    """🟥 --refresh(cache=None)忽略缓存、强制重核——改了核验逻辑后避免复用脏证据。"""
    extraction = _extraction(demo_cfg)
    first = verify(extraction, demo_cfg, checker=OfflineChecker(), now=FIXED_NOW)
    # 即便缓存里有新鲜记录,传 cache=None 也全部重核
    refreshed = verify(extraction, demo_cfg, checker=OfflineChecker(), now=FIXED_NOW, cache=None)
    assert len(refreshed) == len(first) == 3


def test_verify_rechecks_stale_cache(demo_cfg: FrozenConfig) -> None:
    extraction = _extraction(demo_cfg)
    first = verify(extraction, demo_cfg, checker=OfflineChecker(), now=FIXED_NOW)
    cache = {r.normalized_url: r for r in first}
    later = verify(
        extraction,
        demo_cfg,
        checker=OfflineChecker(),
        now="2026-07-10T00:00:00Z",
        cache=cache,  # 30 天后,超过 7 天 TTL
    )
    assert len(later) == 3


def test_unreachable_recorded_as_fact(demo_cfg: FrozenConfig) -> None:
    extraction = _extraction(demo_cfg)
    records = verify(extraction, demo_cfg, checker=_DownChecker(), now=FIXED_NOW)
    assert all(r.reachable is False for r in records)


def test_cache_not_reused_across_different_methods(demo_cfg: FrozenConfig) -> None:
    """换核验实现(method 不同)→ 缓存不复用,必须重核验(口径不能静默混用)。"""
    extraction = _extraction(demo_cfg)
    offline = verify(extraction, demo_cfg, checker=OfflineChecker(), now=FIXED_NOW)
    cache = {r.normalized_url: r for r in offline}
    rechecked = verify(extraction, demo_cfg, checker=_DownChecker(), now=FIXED_NOW, cache=cache)
    assert len(rechecked) == 3
    assert all(r.check_method == "http-head" for r in rechecked)


def test_evidence_record_is_url_keyed(demo_cfg: FrozenConfig) -> None:
    """同一 URL 在多条回答里出现,也只核验一次(URL 主键)。"""
    r0 = make_response(
        question_id="q1",
        citations=(Citation(url="https://alphapacks.example/p", position=0),),
    )
    r1 = make_response(
        question_id="q2",
        citations=(Citation(url="https://alphapacks.example/p/", position=0),),  # 规范化后同一 URL
    )
    extraction = extract([r0, r1], demo_cfg)
    records = verify(extraction, demo_cfg, checker=OfflineChecker(), now=FIXED_NOW)
    assert len(records) == 1
    assert isinstance(records[0], EvidenceRecord)
