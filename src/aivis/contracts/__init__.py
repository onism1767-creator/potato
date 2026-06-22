"""contracts —— 🟥 载重墙:所有跨模块数据形状的唯一定义处(CLAUDE.md §2)。

禁止在本包之外就近发明新的数据结构;需要新字段先改这里(且先问人)。
本包不依赖 aivis 的任何上层模块(由 import-linter 保证)。
"""

from aivis.contracts.adapter import ProviderAdapter
from aivis.contracts.checker import CheckOutcome, UrlChecker
from aivis.contracts.config import (
    Brand,
    FrozenConfig,
    Intent,
    Question,
    Track,
    VerifyThresholds,
)
from aivis.contracts.records import (
    AttributionBasis,
    BrandMention,
    Citation,
    CitationAttribution,
    ErrorCode,
    EvidenceRecord,
    ExtractionResult,
    HealthSummary,
    Interval,
    Light,
    Metrics,
    OwnedOrEarned,
    RawResponse,
    ResponseRef,
    RunManifest,
    ScoreBundle,
    Tier,
    TierAssignment,
)

__all__ = [
    "AttributionBasis",
    "Brand",
    "BrandMention",
    "CheckOutcome",
    "Citation",
    "CitationAttribution",
    "ErrorCode",
    "EvidenceRecord",
    "ExtractionResult",
    "FrozenConfig",
    "HealthSummary",
    "Intent",
    "Interval",
    "Light",
    "Metrics",
    "OwnedOrEarned",
    "ProviderAdapter",
    "Question",
    "RawResponse",
    "ResponseRef",
    "RunManifest",
    "ScoreBundle",
    "Tier",
    "TierAssignment",
    "Track",
    "UrlChecker",
    "VerifyThresholds",
]
