"""原始层红线测试:append-only + 幂等(CLAUDE.md §10.5 第 6 条)。"""

from pathlib import Path

from aivis.storage.raw import RawStore
from tests.conftest import make_response

_FORBIDDEN_VERBS = ("update", "overwrite", "delete", "remove", "clear", "truncate", "replace")


def test_raw_store_has_no_mutation_methods(tmp_path: Path) -> None:
    """raw store 的公开 API 面上不存在任何改写已有记录的方法。"""
    store = RawStore(tmp_path)
    public = [name for name in dir(store) if not name.startswith("_")]
    assert public, "raw store 应有公开方法"
    for name in public:
        assert not any(verb in name.lower() for verb in _FORBIDDEN_VERBS), (
            f"raw store 不许提供改写方法: {name}"
        )
    assert all(name.startswith(("append_", "read_")) for name in public), (
        f"raw store 只许 append_*/read_*: {public}"
    )


def test_append_and_read_roundtrip(tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    r1 = make_response(question_id="q1", run_index=0, answer_text="hello")
    r2 = make_response(question_id="q1", run_index=1, answer_text="world")
    assert store.append_response(r1) is True
    assert store.append_response(r2) is True
    got = store.read_responses("run-test")
    assert got == [r1, r2]


def test_append_is_idempotent_on_business_key(tmp_path: Path) -> None:
    """同 (run_id, question_id, run_index) 重复 append 被跳过——续跑不写重。"""
    store = RawStore(tmp_path)
    first = make_response(answer_text="original")
    duplicate = make_response(answer_text="DIFFERENT TEXT, SAME KEY")
    assert store.append_response(first) is True
    assert store.append_response(duplicate) is False
    got = store.read_responses()
    assert len(got) == 1
    assert got[0].answer_text == "original"  # 已写入的原始记录不被改写


def test_idempotency_survives_reopen(tmp_path: Path) -> None:
    """重开 store(进程重启/续跑场景)后幂等键依然生效。"""
    RawStore(tmp_path).append_response(make_response())
    reopened = RawStore(tmp_path)
    assert reopened.append_response(make_response()) is False
    assert len(reopened.read_responses()) == 1


def test_existing_file_is_only_appended(tmp_path: Path) -> None:
    """物理层面验证:第二次 append 后,文件以原内容为前缀(只追加)。"""
    store = RawStore(tmp_path)
    store.append_response(make_response(run_index=0))
    before = (tmp_path / "raw_responses.jsonl").read_text(encoding="utf-8")
    store.append_response(make_response(run_index=1))
    after = (tmp_path / "raw_responses.jsonl").read_text(encoding="utf-8")
    assert after.startswith(before)
