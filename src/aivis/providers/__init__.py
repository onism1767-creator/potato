"""providers —— 引擎适配器(全部藏在 contracts.adapter.ProviderAdapter 后面)。

mock:零成本确定性假引擎(默认);anthropic:真 Claude API web_search 探测。
v0.2:openai / gemini / perplexity 各一个适配器模块;统一工厂在 registry,目录在 catalog。
2026-11:加 grok(xAI;复用 openai SDK 指向 api.x.ai,未新增依赖)。
"""

from aivis.contracts.adapter import FatalProviderError
from aivis.providers.anthropic import AnthropicAdapter
from aivis.providers.mock import MockAdapter

__all__ = ["AnthropicAdapter", "FatalProviderError", "MockAdapter"]
