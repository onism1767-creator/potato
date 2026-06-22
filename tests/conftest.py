"""共享夹具:小型冻结配置 + 手工原始响应构造器(全部确定性)。"""

import json

import pytest

from aivis.contracts.config import Brand, FrozenConfig, Question
from aivis.contracts.records import Citation, ErrorCode, RawResponse

FIXED_NOW = "2026-06-10T00:00:00Z"


@pytest.fixture()
def demo_cfg() -> FrozenConfig:
    """两品牌三问题的微型冻结配置(黄金测试的标准零件)。"""
    return FrozenConfig(
        version="test-v1",
        prompt_version="test-questions-v1",
        competitor_set_version="test-brands-v1",
        focal_domain=None,
        peers=(
            Brand(
                name="Alpha Packs",
                aliases=("AlphaPacks",),
                ambiguous_aliases=("AP",),
                domains=("alphapacks.example",),
            ),
            Brand(
                name="Bravo Gear",
                aliases=(),
                ambiguous_aliases=("Bravo",),
                domains=("bravogear.example",),
            ),
        ),
        competitors_on=False,
        questions=(
            Question(id="q1", intent="discovery", text="Best packs?"),
            Question(id="q2", intent="comparison", text="Compare pack brands?"),
            Question(id="q3", intent="brand_defense", text="Is Alpha Packs good?"),
        ),
        tier="standard",
        locale="en-US",
        region="US",
        temperature=0.0,
        n_runs=2,
        max_uses=3,
        budget_cap_usd=1.0,
    )


def make_response(
    *,
    run_id: str = "run-test",
    question_id: str = "q1",
    run_index: int = 0,
    answer_text: str = "",
    citations: tuple[Citation, ...] = (),
    finished_ok: bool = True,
    error_code: ErrorCode | None = None,
    search_count: int = 1,
) -> RawResponse:
    return RawResponse(
        run_id=run_id,
        question_id=question_id,
        run_index=run_index,
        answer_text=answer_text,
        citations=citations,
        search_count=search_count,
        model="mock-model-standard",
        finished_ok=finished_ok,
        error_code=error_code,
        error_detail=None,
        raw_json=json.dumps({"test": True}),
        timestamp=FIXED_NOW,
    )
