"""证据层 ②:按 normalized_url 去重的核验缓存(可重算,与 ① 解耦留档)。

同一 URL 跨问题/跨轮反复出现,核验一次即可复用(限速友好;v2 真抓页面时省钱,
对应"先核 headline、长尾按需"的成本红线)。读取时同一 URL 取最新一条。
"""

import json
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
        with self._path.open("a", encoding="utf-8") as f:
            for r in records:
                f.write(r.model_dump_json() + "\n")

    def load(self) -> dict[str, EvidenceRecord]:
        """读取核验缓存:normalized_url -> 最新记录。"""
        latest: dict[str, EvidenceRecord] = {}
        if not self._path.exists():
            return latest
        with self._path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    record = EvidenceRecord.model_validate(json.loads(line))
                    latest[record.normalized_url] = record
        return latest
