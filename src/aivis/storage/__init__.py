"""storage —— 三类数据的物理边界(CLAUDE.md §1):

raw(①只追加)/ evidence(②可重算的 URL 核验缓存)/ analysis(③完全派生)。
本包不反向依赖 pipeline / providers / cli(由 import-linter 保证)。
"""

from aivis.storage.analysis import AnalysisStore
from aivis.storage.evidence import EvidenceStore
from aivis.storage.raw import RawStore

__all__ = ["AnalysisStore", "EvidenceStore", "RawStore"]
