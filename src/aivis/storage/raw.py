"""原始层 ①:append-only 的唯一真相源(🟥 CLAUDE.md §1/§10.5 第 6 条)。

本类只暴露 append_* / read_*,不提供任何 update / overwrite / delete 方法;
配套单元测试断言 API 面上没有改写动词。append 以业务键幂等:
  - RawResponse: (run_id, question_id, run_index)
  - RunManifest: run_id
重复 append 静默跳过 —— 真实运行(花钱)崩溃后可用同一 run_id 续跑,不写重、不重烧预算。
"""

import json
import warnings
from pathlib import Path

from aivis.contracts.records import RawResponse, RunLock, RunManifest

_RESPONSES_FILE = "raw_responses.jsonl"
_MANIFESTS_FILE = "run_manifests.jsonl"
_LOCKS_FILE = "run_locks.jsonl"


class RawStore:
    """原始观测存储(JSONL,只追加)。"""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)
        self._responses_path = root / _RESPONSES_FILE
        self._manifests_path = root / _MANIFESTS_FILE
        self._locks_path = root / _LOCKS_FILE
        self._response_keys: set[tuple[str, str, int]] = {
            (r.run_id, r.question_id, r.run_index) for r in self.read_responses()
        }
        self._manifest_keys: set[str] = {m.run_id for m in self.read_manifests()}
        self._lock_keys: set[str] = {lock.run_id for lock in self.read_locks()}

    def append_response(self, response: RawResponse) -> bool:
        """追加一条原始响应;业务键已存在则跳过。返回是否实际写入。"""
        key = (response.run_id, response.question_id, response.run_index)
        if key in self._response_keys:
            return False
        self._append_line(self._responses_path, response.model_dump_json())
        self._response_keys.add(key)
        return True

    def append_manifest(self, manifest: RunManifest) -> bool:
        """追加一条运行清单;run_id 已存在则跳过。返回是否实际写入。"""
        if manifest.run_id in self._manifest_keys:
            return False
        self._append_line(self._manifests_path, manifest.model_dump_json())
        self._manifest_keys.add(manifest.run_id)
        return True

    def read_responses(self, run_id: str | None = None) -> list[RawResponse]:
        """读取原始响应;可按 run_id 过滤。"""
        records = [
            RawResponse.model_validate(obj) for obj in self._read_lines(self._responses_path)
        ]
        if run_id is not None:
            records = [r for r in records if r.run_id == run_id]
        return records

    def read_response_keys(self, run_id: str) -> set[tuple[str, int]]:
        """已存储的 (question_id, run_index) 键集合。

        probe 用它在调 provider **之前**跳过已存在的探测——续跑不重烧预算
        (只去重存储救不了钱,付费调用必须在发生前就拦)。
        """
        return {(qid, ri) for (rid, qid, ri) in self._response_keys if rid == run_id}

    def read_manifests(self) -> list[RunManifest]:
        """读取全部运行清单(按写入顺序)。"""
        return [RunManifest.model_validate(obj) for obj in self._read_lines(self._manifests_path)]

    def append_lock(self, lock: RunLock) -> bool:
        """追加一把配置锁;run_id 已有锁则跳过(append-only)。返回是否实际写入。"""
        if lock.run_id in self._lock_keys:
            return False
        self._append_line(self._locks_path, lock.model_dump_json())
        self._lock_keys.add(lock.run_id)
        return True

    def read_locks(self) -> list[RunLock]:
        """读取全部配置锁。"""
        return [RunLock.model_validate(obj) for obj in self._read_lines(self._locks_path)]

    def read_lock(self, run_id: str) -> RunLock | None:
        """读取某 run_id 的配置锁(无则 None)。"""
        for lock in self.read_locks():
            if lock.run_id == run_id:
                return lock
        return None

    @staticmethod
    def _append_line(path: Path, line: str) -> None:
        # 上次写到一半被杀(关窗口/断电/磁盘满)会留下没有换行的半截末行。先补一个换行把它隔开,
        # 否则这条新记录会被粘在半截行后面一起报废。只追加、不截断、不改写已有内容。
        torn_tail = False
        if path.exists() and path.stat().st_size > 0:
            with path.open("rb") as existing:
                existing.seek(-1, 2)
                torn_tail = existing.read(1) != b"\n"
        with path.open("a", encoding="utf-8") as f:
            f.write(("\n" if torn_tail else "") + line + "\n")

    @staticmethod
    def _read_lines(path: Path) -> list[dict[str, object]]:
        if not path.exists():
            return []
        out: list[dict[str, object]] = []
        with path.open("r", encoding="utf-8") as f:
            for number, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    # 半截行(写入中途被中断的残留):那次调用从未完整落盘,续跑会重问它。
                    # 跳过并显式告警 —— 不能让一行残渣把整个数据目录(含别的 run)全部锁死。
                    warnings.warn(
                        f"{path.name} 第 {number} 行不是完整的 JSON(多半是写入中途被中断),已跳过",
                        RuntimeWarning,
                        stacklevel=2,
                    )
                    continue
                assert isinstance(obj, dict)
                out.append(obj)
        return out
