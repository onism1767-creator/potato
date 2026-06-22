"""providers —— 引擎适配器(全部藏在 contracts.adapter.ProviderAdapter 后面)。

mock:零成本确定性假引擎(默认);anthropic:真 Claude API web_search 探测。
OpenAI / Gemini 不建文件——接口已由 Protocol 预留,写占位实现也算"提前实现"。
"""

from aivis.providers.anthropic import AnthropicAdapter, FatalProviderError
from aivis.providers.mock import MockAdapter

__all__ = ["AnthropicAdapter", "FatalProviderError", "MockAdapter"]
