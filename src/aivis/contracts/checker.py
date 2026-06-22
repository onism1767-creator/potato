"""UrlChecker Protocol —— 核验器的封装边界(与 ProviderAdapter 同级的接口预留)。

verify 流水线只认本接口,不关心核验"怎么做"。升级路径(BUILD_SPEC §8 step 11):
真页面抓取 + 摘录支持性检查 = 新增一个实现类(含 HTML 消毒 + 域名 allow/blocklist),
verify / score / extract / storage 全部零改动。

🟥 抓取实现的安全红线(CLAUDE.md §5,写给未来的实现类):
所有网页内容一律不可信(prompt-injection-safe);snippet 必须经规则层消毒,
绝不把生 HTML 当指令传递;遵守 robots/ToS,只存短摘录不镜像整页。
"""

from typing import Protocol

from pydantic import BaseModel, ConfigDict

# 核验方式标识(属契约:进 EvidenceRecord.check_method,verify 与 score 共用同一词汇,
# 放契约层 → 纯层 score 可引用而不必 import verify 的网络依赖)
CHECK_OFFLINE = "offline-assumed"
CHECK_HTTP_HEAD = "http-head"
CHECK_PAGE_FETCH = "page-fetch"


class CheckOutcome(BaseModel):
    """一次 URL 核验的结果(EvidenceRecord 的事实来源)。"""

    model_config = ConfigDict(frozen=True)

    reachable: bool
    method: str  # CHECK_OFFLINE / CHECK_HTTP_HEAD / CHECK_PAGE_FETCH
    snippet: str | None = None  # 消毒后的短摘录;不抓正文的实现恒为 None
    # 抓取核验下:页面正文里确实出现清晰名的**品牌**集合(摘录支持性检查的依据)。
    # 不抓正文的实现(offline/head)恒为空——无法声称"页面提到了品牌"。
    brands_on_page: tuple[str, ...] = ()


class UrlChecker(Protocol):
    """核验器接口:verify 只依赖它,实现可替换。"""

    @property
    def method(self) -> str:
        """核验方式标识(诚实入档 + 证据缓存按它判断可否复用)。"""
        ...

    def check(self, url: str) -> CheckOutcome:
        """核验单个(规范化后的)URL。实现自己负责超时与异常兜底,不许向外抛。"""
        ...
