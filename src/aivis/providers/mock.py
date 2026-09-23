"""mock 适配器:无密钥、零成本、完全确定性(mock-by-default,CLAUDE.md §6)。

混合形态(已拍板):
  - fixture 模式:question_id 在 fixture 目录有同名 JSON → 按 run_index 取预设响应
    (黄金测试用,可精确覆盖干净命中/歧义/失败三种情形);
  - 模板模式:其余 question_id 走确定性生成 —— 种子 = sha256(prompt_version |
    question_id | run_index),同输入永远同输出。
时间戳为固定假时刻(mock 本身就是假数据,换真时钟反而破坏黄金测试的确定性)。
"""

import hashlib
import json
import random
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from aivis.contracts.adapter import ModelInfo, ProviderInfo
from aivis.contracts.config import Brand, FrozenConfig, Question
from aivis.contracts.records import Citation, ErrorCode, RawResponse

_MOCK_TIMESTAMP = "2026-01-01T00:00:00Z"

PROVIDER_INFO = ProviderInfo(
    name="mock", label="Mock (free demo)", env_key="", endpoint="(none: nothing is sent anywhere)"
)
# mock 的"模型目录":三档假模型,零成本;model id = mock-model-<tier>(与历史黄金一致)
MODELS: tuple[ModelInfo, ...] = tuple(
    ModelInfo(
        provider="mock",
        model=f"mock-model-{tier}",
        label=f"Mock {tier}",
        tier=tier,
        blurb="Deterministic built-in sample data. $0, no key, nothing sent to any API.",
    )
    for tier in ("economy", "standard", "high")
)
_TIER_OF_MODEL = {m.model: m.tier for m in MODELS}


class _FixtureRun(BaseModel):
    """fixture 文件里单轮响应的形状(provider 内部文件格式,不跨模块)。"""

    model_config = ConfigDict(frozen=True)

    answer_text: str = ""
    citations: tuple[Citation, ...] = ()
    finished_ok: bool = True
    error_code: ErrorCode | None = None
    error_detail: str | None = None
    search_count: int = 1


class _Fixture(BaseModel):
    model_config = ConfigDict(frozen=True)

    runs: tuple[_FixtureRun, ...]


class MockAdapter:
    """确定性假引擎(实现 contracts.adapter.ProviderAdapter)。"""

    def __init__(
        self, tier: str = "standard", fixture_dir: Path | None = None, *, model: str | None = None
    ) -> None:
        # model(精确 id)优先于 tier;未知 id 也接受(mock 不挑模型),tier 从 id 反推或沿用
        if model is not None:
            tier = _TIER_OF_MODEL.get(model, tier)
        self._tier = tier
        self._model = model if model is not None else f"mock-model-{tier}"
        self._fixture_dir = fixture_dir

    @property
    def provider_name(self) -> str:
        return "mock"

    @property
    def surface_id(self) -> str:
        return "mock_api_web_search"

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def info(self) -> ModelInfo:
        for m in MODELS:
            if m.model == self._model:
                return m
        return ModelInfo(
            provider="mock", model=self._model, label=self._model, tier=self._tier, blurb="mock"
        )

    @property
    def tool_path(self) -> str:
        return "web_search_mock"

    @property
    def tool_version(self) -> str | None:
        return "mock-1"

    def estimate_cost_usd(self, n_calls: int, cfg: FrozenConfig) -> float:
        return 0.0

    def actual_cost_usd(self, response: RawResponse) -> float:
        return 0.0

    def ask(
        self, question: Question, cfg: FrozenConfig, *, run_id: str, run_index: int
    ) -> RawResponse:
        fixture_run = self._load_fixture_run(question.id, run_index)
        spec = fixture_run if fixture_run is not None else self._generate(question, cfg, run_index)
        payload = {
            "mock": True,
            "question_id": question.id,
            "run_index": run_index,
            "spec": spec.model_dump(),
        }
        return RawResponse(
            run_id=run_id,
            question_id=question.id,
            run_index=run_index,
            answer_text=spec.answer_text,
            citations=spec.citations,
            search_count=spec.search_count,
            model=self.model_name,
            finished_ok=spec.finished_ok,
            error_code=spec.error_code,
            error_detail=spec.error_detail,
            raw_json=json.dumps(payload, ensure_ascii=False, sort_keys=True),
            timestamp=_MOCK_TIMESTAMP,
        )

    # ---------------------------------------------------------- fixture 模式

    def _load_fixture_run(self, question_id: str, run_index: int) -> _FixtureRun | None:
        if self._fixture_dir is None:
            return None
        path = self._fixture_dir / f"{question_id}.json"
        if not path.exists():
            return None
        fixture = _Fixture.model_validate_json(path.read_text(encoding="utf-8"))
        return fixture.runs[run_index % len(fixture.runs)]

    # ---------------------------------------------------------- 模板模式

    @staticmethod
    def _rng(cfg: FrozenConfig, question_id: str, run_index: int) -> random.Random:
        seed_src = f"{cfg.prompt_version}|{question_id}|{run_index}"
        seed = int.from_bytes(hashlib.sha256(seed_src.encode("utf-8")).digest()[:8], "big")
        return random.Random(seed)

    def _generate(self, question: Question, cfg: FrozenConfig, run_index: int) -> _FixtureRun:
        """确定性模板:提及 2~4 个品牌,造 owned / earned / 无归属三类引用,
        偶尔附带歧义缩写(演练保守计数)。"""
        rng = self._rng(cfg, question.id, run_index)
        brands: list[Brand] = list(cfg.peers)
        # 提及 2~4 个品牌;但品牌数 < 2 时(如焦点-only 骨架)夹到可用范围,避免空 randint。
        # 对 len(brands) >= 2 的配置,lo 恒为 2、行为与旧版一致(不动黄金样本)。
        hi = min(4, len(brands))
        lo = min(2, hi)
        chosen = rng.sample(brands, rng.randint(lo, hi))

        sentences = [f"Here are some options people often recommend for: {question.text}"]
        citations: list[Citation] = []
        position = 0
        for brand in chosen:
            sentences.append(f"{brand.name} is frequently mentioned for its build quality and fit.")
            if rng.random() < 0.7:
                citations.append(
                    Citation(
                        url=f"https://www.{brand.domains[0]}/products?utm_source=mock",
                        title=f"{brand.name} | Official Site",
                        cited_text=f"Explore {brand.name} gear.",
                        position=position,
                    )
                )
                position += 1
            if rng.random() < 0.5:
                slug = brand.name.lower().replace(" ", "-")
                citations.append(
                    Citation(
                        url=f"https://gear-review-weekly.example/reviews/{slug}",
                        title=f"{brand.name} review: tested on trail",
                        cited_text=f"We tested the latest {brand.name} pack for 3 weeks.",
                        position=position,
                    )
                )
                position += 1
        first = chosen[0]
        if first.ambiguous_aliases and rng.random() < 0.3:
            sentences.append(f"Some forums refer to it simply as {first.ambiguous_aliases[0]}.")
        if rng.random() < 0.4:
            citations.append(
                Citation(
                    url="https://outdoor-news.example/roundup/best-picks",
                    title="Seasonal gear roundup",
                    cited_text="A seasonal roundup of outdoor gear.",
                    position=position,
                )
            )
        return _FixtureRun(
            answer_text=" ".join(sentences),
            citations=tuple(citations),
            finished_ok=True,
            error_code=None,
            error_detail=None,
            search_count=rng.randint(1, 3),
        )
