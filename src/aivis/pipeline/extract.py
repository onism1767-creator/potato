"""extract —— 🟥 纯函数层:从 RawResponse 抽取文本提及与引用归属。

禁止网络/磁盘/时钟/未播种随机(由 import-linter + 确定性测试保证)。
算法要点(BUILD_SPEC §6,不许走样):
  - URL 规范化 → 域名 → owned/earned(按 Brand.domains 后缀匹配);
  - 文本提及用确定性别名匹配(word-boundary,大小写不敏感);
  - 保守计数:只有清晰品牌名 counted=True;缩写/歧义 counted=False 进 extra data。
"""

from collections.abc import Iterable
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from aivis.contracts.config import Brand, FrozenConfig
from aivis.contracts.records import (
    AttributionBasis,
    BrandMention,
    CitationAttribution,
    ExtractionResult,
    OwnedOrEarned,
    RawResponse,
    ResponseRef,
)
from aivis.matching import find_positions  # 单一匹配口径(verify/configcheck 共用 matching)

# 常见跟踪参数:规范化时剔除(短链/重定向解析是 verify 的事,这里只做纯字符串规范化)
_TRACKING_PARAMS = ("utm_", "gclid", "fbclid", "msclkid", "mc_cid", "mc_eid")


def normalize_url(url: str) -> str:
    """确定性 URL 规范化:小写 scheme/host、去默认端口、去 fragment、去跟踪参数、
    剩余 query 排序、去尾斜杠。解析失败时原样返回(strip 后)。"""
    raw = url.strip()
    parsed = urlparse(raw)
    if not parsed.scheme or not parsed.netloc:
        return raw
    host = parsed.netloc.lower()
    scheme = parsed.scheme.lower()
    if (scheme == "http" and host.endswith(":80")) or (scheme == "https" and host.endswith(":443")):
        host = host.rsplit(":", 1)[0]
    query_pairs = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if not (k.lower().startswith(_TRACKING_PARAMS[0]) or k.lower() in _TRACKING_PARAMS[1:])
    ]
    query = urlencode(sorted(query_pairs))
    path = parsed.path.rstrip("/") if parsed.path not in ("", "/") else ""
    return urlunparse((scheme, host, path, parsed.params, query, ""))


def canonical_domain(url: str) -> str:
    """从(规范化或原始)URL 提取规范域名:小写、去端口、去 www. 前缀。"""
    host = urlparse(url.strip()).netloc.lower()
    if not host:
        return ""
    host = host.rsplit(":", 1)[0] if ":" in host else host
    return host.removeprefix("www.")


def owner_of_domain(domain: str, brands: Iterable[Brand]) -> Brand | None:
    """域名属于哪个品牌的自有域(含子域后缀匹配);不属于任何品牌返回 None。"""
    for brand in brands:
        for d in brand.domains:
            d = d.lower().removeprefix("www.")
            if domain == d or domain.endswith("." + d):
                return brand
    return None


def _mention_for(response: RawResponse, brand: Brand) -> BrandMention:
    """单品牌 × 单回答的提及观测(保守计数)。"""
    text = response.answer_text
    clear_hits = find_positions(text, (brand.name, *brand.aliases))
    if clear_hits:
        return BrandMention(
            run_id=response.run_id,
            question_id=response.question_id,
            run_index=response.run_index,
            brand=brand.name,
            mentioned=True,
            first_position=clear_hits[0][0],
            mention_count=len(clear_hits),
            counted=True,
            reason=None,
            answer_length=len(text),
        )
    ambiguous_hits = find_positions(text, brand.ambiguous_aliases)
    if ambiguous_hits:
        return BrandMention(
            run_id=response.run_id,
            question_id=response.question_id,
            run_index=response.run_index,
            brand=brand.name,
            mentioned=True,
            first_position=ambiguous_hits[0][0],
            mention_count=len(ambiguous_hits),
            counted=False,
            reason=f"ambiguous_alias:{ambiguous_hits[0][1]}",
            answer_length=len(text),
        )
    return BrandMention(
        run_id=response.run_id,
        question_id=response.question_id,
        run_index=response.run_index,
        brand=brand.name,
        mentioned=False,
        first_position=None,
        mention_count=0,
        counted=True,  # 对"缺席"的有效观测,进覆盖率分母
        reason=None,
        answer_length=len(text),
    )


def _attributions_for(
    response: RawResponse, brands: tuple[Brand, ...]
) -> list[CitationAttribution]:
    """单回答的全部引用归属。

    规则(确定性,按优先级):
      1. 域名属于某品牌自有域 → 该品牌 owned(basis=domain),且不再给其他品牌 earned
         (竞品官网标题里出现别家名字不算别家的 earned,保守);
      2. 第三方域名且引用元数据(title/cited_text)含品牌清晰名 → 每个命中品牌一条
         earned(basis=cited_metadata);只命中歧义名 → 记录但 counted=False;
      3. 谁都不沾 → brand=None, counted=False, reason="unattributed"。
    """
    out: list[CitationAttribution] = []
    for citation in response.citations:
        norm = normalize_url(citation.url)
        domain = canonical_domain(norm)

        def _record(
            brand: str | None,
            owned_or_earned: OwnedOrEarned | None,
            basis: AttributionBasis | None,
            counted: bool,
            reason: str | None,
            *,
            _url: str = citation.url,
            _norm: str = norm,
            _domain: str = domain,
        ) -> CitationAttribution:
            return CitationAttribution(
                run_id=response.run_id,
                question_id=response.question_id,
                run_index=response.run_index,
                url=_url,
                normalized_url=_norm,
                canonical_domain=_domain,
                brand=brand,
                owned_or_earned=owned_or_earned,
                basis=basis,
                counted=counted,
                reason=reason,
            )

        owner = owner_of_domain(domain, brands)
        if owner is not None:
            out.append(_record(owner.name, "owned", "domain", True, None))
            continue
        metadata_text = " ".join(filter(None, (citation.title, citation.cited_text)))
        matched = False
        for brand in brands:
            if find_positions(metadata_text, (brand.name, *brand.aliases)):
                out.append(_record(brand.name, "earned", "cited_metadata", True, None))
                matched = True
            else:
                ambiguous = find_positions(metadata_text, brand.ambiguous_aliases)
                if ambiguous:
                    out.append(
                        _record(
                            brand.name,
                            "earned",
                            "cited_metadata",
                            False,
                            f"ambiguous_alias:{ambiguous[0][1]}",
                        )
                    )
                    matched = True
        if not matched:
            out.append(_record(None, None, None, False, "unattributed"))
    return out


def extract(responses: Iterable[RawResponse], cfg: FrozenConfig) -> ExtractionResult:
    """对一次运行的全部原始响应做抽取(纯;同输入恒同输出)。"""
    responses = sorted(responses, key=lambda r: (r.question_id, r.run_index))
    if not responses:
        raise ValueError("没有可抽取的原始响应")
    run_ids = {r.run_id for r in responses}
    if len(run_ids) != 1:
        raise ValueError(f"一次 extract 只处理一个 run,收到: {sorted(run_ids)}")

    refs: list[ResponseRef] = []
    mentions: list[BrandMention] = []
    citations: list[CitationAttribution] = []
    for resp in responses:
        refs.append(
            ResponseRef(
                question_id=resp.question_id,
                run_index=resp.run_index,
                finished_ok=resp.finished_ok,
                error_code=resp.error_code,
            )
        )
        if not resp.finished_ok:
            continue  # 失败显式记录在 refs,不冒充"空回答"
        mentions.extend(_mention_for(resp, brand) for brand in cfg.peers)
        citations.extend(_attributions_for(resp, cfg.peers))

    return ExtractionResult(
        run_id=next(iter(run_ids)),
        response_refs=tuple(refs),
        mentions=tuple(mentions),
        citations=tuple(citations),
    )
