"""registry —— 表面(provider:model)→ 适配器实例的唯一工厂。

🟥 CLAUDE.md §4:引擎名分支只许出现在这里一处;pipeline / GUI / CLI 一律经本模块取适配器。
SDK 只在对应分支里惰性 import:选 mock 不加载任何付费 SDK,某家 SDK 缺失只影响那一家。
"""

from aivis.contracts.adapter import ModelInfo, ProviderAdapter, SurfaceSpec
from aivis.providers.catalog import PROVIDERS, default_model, find_model


def resolve(spec: SurfaceSpec, *, tier: str = "standard") -> ModelInfo:
    """把 provider(+model)解析成目录条目;model 缺省取该引擎在 tier 档的默认模型。"""
    if spec.provider not in PROVIDERS:
        raise ValueError(f"未知引擎 {spec.provider!r};可选: {sorted(PROVIDERS)}")
    model = spec.model if spec.model is not None else default_model(spec.provider, tier)
    return find_model(spec.provider, model)


def make_adapter(spec: SurfaceSpec, *, tier: str = "standard") -> ProviderAdapter:
    """按表面构造适配器(唯一允许按引擎名分支的地方)。"""
    info = resolve(spec, tier=tier)
    if info.provider == "mock":
        from aivis.providers.mock import MockAdapter

        return MockAdapter(model=info.model)
    if info.provider == "anthropic":
        from aivis.providers.anthropic import AnthropicAdapter

        return AnthropicAdapter(model=info.model)
    if info.provider == "openai":
        from aivis.providers.openai import OpenAIAdapter

        return OpenAIAdapter(model=info.model)
    if info.provider == "gemini":
        from aivis.providers.gemini import GeminiAdapter

        return GeminiAdapter(model=info.model)
    if info.provider == "perplexity":
        from aivis.providers.perplexity import PerplexityAdapter, PerplexitySearchAdapter

        if info.surface_kind == "search_only":
            return PerplexitySearchAdapter()
        return PerplexityAdapter(model=info.model)
    raise NotImplementedError(f"引擎 {info.provider!r} 的适配器尚未接入")
