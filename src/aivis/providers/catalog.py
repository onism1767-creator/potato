"""catalog —— 五家引擎的模型目录(单一真相源;纯数据,不 import 任何 SDK)。

GUI 的"选测哪些 AI"、CLI 的 `aivis models`、注册表的默认档位都从这里读。
单价与计价逻辑**不在此**:按 CLAUDE.md §6.3 由各适配器模块按 usage 计价。
模型 id 均对照官方模型页核实(前四家 2026-09-22;Grok 2026-11-19);status:
  current = 官方现役;legacy = 官方总览页已下架但接口仍可调(随时可能停);preview = 预览版。
"""

from aivis.contracts.adapter import ModelInfo, ProviderInfo

PROVIDERS: dict[str, ProviderInfo] = {
    "mock": ProviderInfo(
        name="mock",
        label="Mock (free demo)",
        env_key="",
        endpoint="(nothing is sent anywhere)",
    ),
    "anthropic": ProviderInfo(
        name="anthropic",
        label="Claude (Anthropic)",
        env_key="ANTHROPIC_API_KEY",
        endpoint="api.anthropic.com",
    ),
    "openai": ProviderInfo(
        name="openai",
        label="ChatGPT models (OpenAI)",
        env_key="OPENAI_API_KEY",
        endpoint="api.openai.com",
    ),
    "gemini": ProviderInfo(
        name="gemini",
        label="Gemini (Google)",
        env_key="GEMINI_API_KEY",
        endpoint="generativelanguage.googleapis.com",
    ),
    "perplexity": ProviderInfo(
        name="perplexity",
        label="Perplexity",
        env_key="PERPLEXITY_API_KEY",
        endpoint="api.perplexity.ai",
    ),
    "grok": ProviderInfo(
        name="grok",
        label="Grok (xAI)",
        env_key="XAI_API_KEY",
        endpoint="api.x.ai",
    ),
}


def _m(provider: str, model: str, label: str, tier: str, blurb: str, **kw: object) -> ModelInfo:
    return ModelInfo.model_validate(
        {"provider": provider, "model": model, "label": label, "tier": tier, "blurb": blurb, **kw}
    )


# 顺序 = GUI 展示顺序(每家内按 便宜 → 贵)。
MODELS: tuple[ModelInfo, ...] = (
    # ---- mock:零成本假引擎(与历史黄金一致的三档 id)----
    _m("mock", "mock-model-economy", "Mock economy", "economy", "Built-in sample data, $0."),
    _m("mock", "mock-model-standard", "Mock standard", "standard", "Built-in sample data, $0."),
    _m("mock", "mock-model-high", "Mock high", "high", "Built-in sample data, $0."),
    # ---- Claude(Anthropic)----
    _m(
        "anthropic",
        "claude-haiku-4-5",
        "Claude Haiku 4.5",
        "economy",
        "Cheapest & fastest Claude, lower depth: a quick first look.",
    ),
    _m(
        "anthropic",
        "claude-sonnet-4-6",
        "Claude Sonnet 4.6",
        "standard",
        "Previous-generation workhorse; validated with inline citations (2026-06).",
        status="legacy",
    ),
    _m(
        "anthropic",
        "claude-sonnet-5",
        "Claude Sonnet 5",
        "standard",
        "Current workhorse: strong quality at moderate cost.",
        supports_temperature=False,
    ),
    _m(
        "anthropic",
        "claude-opus-4-8",
        "Claude Opus 4.8",
        "high",
        "Previous-generation flagship.",
        status="legacy",
        supports_temperature=False,
    ),
    _m(
        "anthropic",
        "claude-opus-5",
        "Claude Opus 5",
        "high",
        "Current Opus: most capable in the Opus line.",
        supports_temperature=False,
    ),
    _m(
        "anthropic",
        "claude-fable-5-1",
        "Claude Fable 5.1",
        "flagship",
        "Anthropic's most capable model; highest cost; may refuse some questions.",
        supports_temperature=False,
    ),
    # ---- OpenAI(Responses API + web_search;推理模型不接受采样参数)----
    _m(
        "openai",
        "gpt-6-luna",
        "GPT-6 Luna",
        "economy",
        "Current lightweight model (free-tier default in ChatGPT).",
        supports_temperature=False,
    ),
    _m(
        "openai",
        "gpt-6-sol",
        "GPT-6 Sol",
        "standard",
        "Current mainline model (paid default in ChatGPT).",
        supports_temperature=False,
    ),
    _m(
        "openai",
        "gpt-6-astra",
        "GPT-6 Astra",
        "flagship",
        "Current flagship; highest cost.",
        supports_temperature=False,
    ),
    _m(
        "openai",
        "gpt-5.6-luna",
        "GPT-5.6 Luna",
        "economy",
        "Previous-generation lightweight model; removed from the official overview page.",
        status="legacy",
        supports_temperature=False,
    ),
    _m(
        "openai",
        "gpt-5.6-terra",
        "GPT-5.6 Terra",
        "standard",
        "Previous-generation mainline model; removed from the official overview page.",
        status="legacy",
        supports_temperature=False,
    ),
    _m(
        "openai",
        "gpt-5.6-sol",
        "GPT-5.6 Sol",
        "high",
        "Previous-generation paid default; removed from the official overview page.",
        status="legacy",
        supports_temperature=False,
    ),
    # ---- Gemini(Grounding with Google Search;无区域参数)----
    _m(
        "gemini",
        "gemini-3.8-flash",
        "Gemini 3.8 Flash",
        "standard",
        "Latest Flash (Sep 2026).",
        supports_region=False,
    ),
    _m(
        "gemini",
        "gemini-3.7-flash",
        "Gemini 3.7 Flash",
        "standard",
        "Previous Flash (Aug 2026).",
        supports_region=False,
    ),
    _m(
        "gemini",
        "gemini-3.6-flash",
        "Gemini 3.6 Flash",
        "standard",
        "Flash (Jul 2026).",
        supports_region=False,
    ),
    _m(
        "gemini",
        "gemini-3.5-flash",
        "Gemini 3.5 Flash",
        "standard",
        "Flash (May 2026); higher token price than 3.6+.",
        supports_region=False,
    ),
    _m(
        "gemini",
        "gemini-3.1-pro-preview",
        "Gemini 3.1 Pro (preview)",
        "high",
        "Pro preview; strongest Gemini reasoning here.",
        status="preview",
        supports_region=False,
    ),
    # ---- Perplexity(Agent API;Sonar 聊天接口 2026-09-27 停服)----
    _m(
        "perplexity",
        "perplexity/sonar",
        "Sonar",
        "economy",
        "Perplexity's own Sonar model via the Agent API (pinned model id). "
        "Smoke (2026-09): its answers carried no inline citation markers, so citation "
        "metrics may read 0 while mention metrics work.",
    ),
    _m(
        "perplexity",
        "preset:low",
        "Sonar Pro (preset low)",
        "standard",
        "Official replacement for sonar-pro: Agent API preset 'low'. "
        "Underlying model is an OpenAI model and auto-updates; not pinnable.",
        pinned=False,
        supports_temperature=False,
    ),
    _m(
        "perplexity",
        "preset:medium",
        "Sonar Reasoning Pro (preset medium)",
        "high",
        "Official replacement for sonar-reasoning-pro: Agent API preset 'medium'. "
        "Underlying model is an OpenAI model and auto-updates; not pinnable.",
        pinned=False,
        supports_temperature=False,
    ),
    _m(
        "perplexity",
        "search",
        "Perplexity Search API",
        "economy",
        "Pure web search results (title/url/snippet), no generated answer: mention "
        "metrics do not apply; citation metrics = result links.",
        surface_kind="search_only",
        supports_temperature=False,  # 纯搜索接口无采样参数
    ),
    # ---- Grok(xAI Responses API + web_search;无区域参数;id 与单价核自 /v1/language-models,
    #      2026-11-19;未收 grok-build-0.1[编码模型]与 grok-4.20-multi-agent[多代理,另一种表面])----
    _m(
        "grok",
        "grok-4.20-0309-non-reasoning",
        "Grok 4.20 (non-reasoning)",
        "economy",
        "Dated Grok 4.20 snapshot without reasoning; fastest Grok here.",
        supports_region=False,
    ),
    _m(
        "grok",
        "grok-4.20-0309-reasoning",
        "Grok 4.20 (reasoning)",
        "economy",
        "Dated Grok 4.20 snapshot with reasoning.",
        supports_region=False,
    ),
    _m(
        "grok",
        "grok-4.3",
        "Grok 4.3",
        "economy",
        "Lower-cost Grok (light reasoning by default): a quick first look.",
        supports_region=False,
    ),
    _m(
        "grok",
        "grok-4.5",
        "Grok 4.5",
        "standard",
        "Earlier Grok 4.x mainline model; same token price as 4.7.",
        supports_region=False,
    ),
    _m(
        "grok",
        "grok-4.6",
        "Grok 4.6",
        "standard",
        "Previous Grok mainline model; same token price as 4.7.",
        supports_region=False,
    ),
    _m(
        "grok",
        "grok-4.7",
        "Grok 4.7",
        "standard",
        "Current Grok mainline model (reasoning always on). "
        "Smoke (2026-11): its answers carried no inline citation markers, so citation "
        "metrics may read 0 while mention metrics work.",
        supports_region=False,
    ),
)

_BY_KEY: dict[str, ModelInfo] = {m.key: m for m in MODELS}
# 每家 tier → 默认模型(`--tier` 便利选项;显式 `--model` 优先)。standard 档保持经验证的旧默认。
_TIER_DEFAULTS: dict[str, dict[str, str]] = {
    "mock": {
        "economy": "mock-model-economy",
        "standard": "mock-model-standard",
        "high": "mock-model-high",
    },
    "anthropic": {
        "economy": "claude-haiku-4-5",
        "standard": "claude-sonnet-4-6",
        "high": "claude-opus-4-8",
        "flagship": "claude-fable-5-1",
    },
    "openai": {
        "economy": "gpt-6-luna",
        "standard": "gpt-6-sol",
        "high": "gpt-5.6-sol",
        "flagship": "gpt-6-astra",
    },
    "gemini": {
        "economy": "gemini-3.8-flash",
        "standard": "gemini-3.8-flash",
        "high": "gemini-3.1-pro-preview",
    },
    "perplexity": {
        "economy": "perplexity/sonar",
        "standard": "preset:low",
        "high": "preset:medium",
    },
    # 4.5–4.7 同价,standard/high 都取现役主力 4.7
    "grok": {
        "economy": "grok-4.3",
        "standard": "grok-4.7",
        "high": "grok-4.7",
    },
}


def list_models(provider: str | None = None) -> tuple[ModelInfo, ...]:
    return tuple(m for m in MODELS if provider is None or m.provider == provider)


def find_model(provider: str, model: str) -> ModelInfo:
    try:
        return _BY_KEY[f"{provider}:{model}"]
    except KeyError:
        known = ", ".join(m.model for m in list_models(provider)) or "(无此引擎)"
        raise ValueError(f"引擎 {provider!r} 无模型 {model!r};可选: {known}") from None


def default_model(provider: str, tier: str = "standard") -> str:
    """该引擎在某档位的默认模型 id;档位不存在时给出明确错误。"""
    tiers = _TIER_DEFAULTS.get(provider)
    if tiers is None:
        raise ValueError(f"未知引擎 {provider!r};可选: {sorted(_TIER_DEFAULTS)}")
    if tier not in tiers:
        raise ValueError(f"引擎 {provider!r} 无档位 {tier!r};可选: {sorted(tiers)}")
    return tiers[tier]
