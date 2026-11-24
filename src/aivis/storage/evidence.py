"""证据层 ②:按 normalized_url 去重的核验缓存(可重算,与 ① 解耦留档)。

同一 URL 跨问题/跨轮反复出现,核验一次即可复用(限速友好;v2 真抓页面时省钱,
对应"先核 headline、长尾按需"的成本红线)。读取时同一 URL 取最新一条。
"""

import json
import warnings
from pathlib import Path

from aivis.contracts.records import EvidenceRecord

_EVIDENCE_FILE = "evidence.jsonl"


class EvidenceStore:
    """核验证据存储(JSONL;append 写入,读取按 normalized_url 取最新)。"""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)
        self._path = root / _EVIDENCE_FILE

    def save(self, records: list[EvidenceRecord]) -> None:
        """追加一批核验记录(旧记录保留作审计痕迹,读取时被更新的覆盖)。"""
        if not records:
            return
        torn_tail = False  # 上次写到一半被中断留下的半截末行:先补换行隔开,别把新记录粘上去
        if self._path.exists() and self._path.stat().st_size > 0:
            with self._path.open("rb") as existing:
                existing.seek(-1, 2)
                torn_tail = existing.read(1) != b"\n"
        with self._path.open("a", encoding="utf-8") as f:
            if torn_tail:
                f.write("\n")
            for r in records:
                f.write(r.model_dump_json() + "\n")

    def load(self) -> dict[str, EvidenceRecord]:
        """读取核验缓存:normalized_url -> 最新记录。"""
        latest: dict[str, EvidenceRecord] = {}
        if not self._path.exists():
            return latest
        with self._path.open("r", encoding="utf-8") as f:
            for number, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    # 半截行(写入中途被中断):证据层可重算,跳过即可,但要说出来
                    warnings.warn(
                        f"{self._path.name} 第 {number} 行不是完整的 JSON,已跳过(会重新核验)",
                        RuntimeWarning,
                        stacklevel=2,
                    )
                    continue
                record = EvidenceRecord.model_validate(obj)
                latest[record.normalized_url] = record
        return latest
