"""mock 适配器测试:确定性、fixture 覆盖、零成本。"""

import json
from pathlib import Path

from aivis.contracts.config import Brand, FrozenConfig
from aivis.providers.mock import MockAdapter


def test_mock_is_deterministic(demo_cfg: FrozenConfig) -> None:
    """同输入永远同输出(黄金测试的前提)。"""
    a = MockAdapter().ask(demo_cfg.questions[0], demo_cfg, run_id="r", run_index=0)
    b = MockAdapter().ask(demo_cfg.questions[0], demo_cfg, run_id="r", run_index=0)
    assert a == b


def test_mock_varies_across_runs_and_questions(demo_cfg: FrozenConfig) -> None:
    q0, q1 = demo_cfg.questions[0], demo_cfg.questions[1]
    base = MockAdapter().ask(q0, demo_cfg, run_id="r", run_index=0)
    other_run = MockAdapter().ask(q0, demo_cfg, run_id="r", run_index=1)
    other_q = MockAdapter().ask(q1, demo_cfg, run_id="r", run_index=0)
    assert {base.answer_text, other_run.answer_text, other_q.answer_text} != {base.answer_text}


def test_mock_seed_pins_to_prompt_version(demo_cfg: FrozenConfig) -> None:
    """题库版本 bump = 新基线:mock 输出随 prompt_version 改变。"""
    bumped = demo_cfg.model_copy(update={"prompt_version": "test-questions-v2"})
    a = MockAdapter().ask(demo_cfg.questions[0], demo_cfg, run_id="r", run_index=0)
    b = MockAdapter().ask(bumped.questions[0], bumped, run_id="r", run_index=0)
    assert a.answer_text != b.answer_text or a.citations != b.citations


def test_mock_costs_nothing(demo_cfg: FrozenConfig) -> None:
    assert MockAdapter().estimate_cost_usd(10_000, demo_cfg) == 0.0
    resp = MockAdapter().ask(demo_cfg.questions[0], demo_cfg, run_id="r", run_index=0)
    assert MockAdapter().actual_cost_usd(resp) == 0.0  # 真实花费也恒 0


def test_fixture_overrides_template(tmp_path: Path, demo_cfg: FrozenConfig) -> None:
    """fixture 模式:question_id 命中文件时按 run_index 取预设(可注入失败情形)。"""
    fixture = {
        "runs": [
            {"answer_text": "Alpha Packs fixture answer.", "search_count": 2},
            {"finished_ok": False, "error_code": "rate_limit", "search_count": 0},
        ]
    }
    (tmp_path / "q1.json").write_text(json.dumps(fixture), encoding="utf-8")
    adapter = MockAdapter(fixture_dir=tmp_path)
    r0 = adapter.ask(demo_cfg.questions[0], demo_cfg, run_id="r", run_index=0)
    r1 = adapter.ask(demo_cfg.questions[0], demo_cfg, run_id="r", run_index=1)
    assert r0.answer_text == "Alpha Packs fixture answer." and r0.finished_ok
    assert r1.finished_ok is False and r1.error_code == "rate_limit"
    # 未命中 fixture 的问题走确定性模板
    r2 = adapter.ask(demo_cfg.questions[1], demo_cfg, run_id="r", run_index=0)
    assert r2.finished_ok and r2.answer_text


def test_mock_handles_single_brand_config(demo_cfg: FrozenConfig) -> None:
    """回归:焦点-only 骨架只有 1 个品牌时 mock 不应崩(旧 randint(2, 1) 空范围)。"""
    one_brand = demo_cfg.model_copy(
        update={"peers": (Brand(name="Solo", domains=("solo.example",), is_focal=True),)}
    )
    resp = MockAdapter().ask(one_brand.questions[0], one_brand, run_id="r", run_index=0)
    assert resp.finished_ok and resp.answer_text


def test_mock_raw_json_is_preserved(demo_cfg: FrozenConfig) -> None:
    resp = MockAdapter().ask(demo_cfg.questions[0], demo_cfg, run_id="r", run_index=0)
    payload = json.loads(resp.raw_json)
    assert payload["mock"] is True and payload["question_id"] == "q1"
