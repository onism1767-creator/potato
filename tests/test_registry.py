"""模型目录 + 注册表测试(离线、零成本):目录一致性、默认档位、表面解析、工厂分支。"""

import pytest

from aivis.contracts.adapter import SurfaceSpec
from aivis.providers import catalog, registry
from aivis.providers.mock import MockAdapter

EXPECTED_COUNTS = {
    "mock": 3,
    "anthropic": 6,
    "openai": 6,
    "gemini": 5,
    "perplexity": 4,
    "grok": 6,
}


def test_catalog_covers_all_providers_with_unique_keys() -> None:
    keys = [m.key for m in catalog.MODELS]
    assert len(keys) == len(set(keys))
    for provider, n in EXPECTED_COUNTS.items():
        assert provider in catalog.PROVIDERS
        assert len(catalog.list_models(provider)) == n, provider
    assert sum(EXPECTED_COUNTS.values()) == len(catalog.MODELS)


def test_every_tier_default_exists_in_catalog() -> None:
    """默认档位表不能指向目录里没有的模型(改目录忘改默认表 → 这里红)。"""
    for provider in catalog.PROVIDERS:
        for tier in ("economy", "standard", "high"):
            model = catalog.default_model(provider, tier)
            assert catalog.find_model(provider, model).provider == provider


def test_honesty_flags_match_research() -> None:
    """诚实标记与官方文档核实结论一致(2026-09-22):OpenAI 推理模型/新 Claude 拒绝
    temperature;Gemini 无区域参数;Perplexity 预设不可钉;Search 纯搜索。"""
    assert all(not m.supports_temperature for m in catalog.list_models("openai"))
    assert all(not m.supports_region for m in catalog.list_models("gemini"))
    assert not catalog.find_model("anthropic", "claude-fable-5-1").supports_temperature
    assert catalog.find_model("anthropic", "claude-sonnet-4-6").supports_temperature
    presets = [m for m in catalog.list_models("perplexity") if m.model.startswith("preset:")]
    assert presets and all(not m.pinned for m in presets)
    assert catalog.find_model("perplexity", "search").surface_kind == "search_only"


def test_find_model_unknown_lists_alternatives() -> None:
    with pytest.raises(ValueError, match="gemini-3.8-flash"):
        catalog.find_model("gemini", "gemini-9")
    with pytest.raises(ValueError, match="未知引擎"):
        catalog.default_model("bing")
    with pytest.raises(ValueError, match="档位"):
        catalog.default_model("gemini", "flagship")


def test_resolve_uses_tier_default_when_model_omitted() -> None:
    assert registry.resolve(SurfaceSpec(provider="anthropic")).model == "claude-sonnet-4-6"
    assert registry.resolve(SurfaceSpec(provider="anthropic"), tier="high").model == (
        "claude-opus-4-8"
    )
    assert registry.resolve(SurfaceSpec.parse("openai:gpt-6-luna")).label == "GPT-6 Luna"
    with pytest.raises(ValueError, match="未知引擎"):
        registry.resolve(SurfaceSpec(provider="bing"))


def test_make_adapter_mock_and_anthropic() -> None:
    a = registry.make_adapter(SurfaceSpec.parse("mock:mock-model-high"))
    assert isinstance(a, MockAdapter) and a.model_name == "mock-model-high"
    b = registry.make_adapter(SurfaceSpec.parse("anthropic:claude-sonnet-5"))
    assert b.provider_name == "anthropic" and b.model_name == "claude-sonnet-5"
    assert b.info.supports_temperature is False
