# ARCHITECTURE — 为什么这么分层(决策记录)

> 配套阅读:行为约束见 `CLAUDE.md`。
> 本文记录**第一版地基的架构决策与口径定义**,改任何一条 = 改契约级决策,先问人。

## 1. 三层数据 + 菱形流水线

数据分三层、单向流动(CLAUDE.md §1):① 原始观测(append-only)→ ② 核验证据(可重算)→ ③ 分析(完全派生)。

流水线的**真实形状是菱形,不是直线**:

```
            ┌─ extract(纯)──┐
probe → ①raw                ├─→ score(纯)→ report(render 纯 / write IO)
            └─ verify(IO)───┘
```

**决策**:verify 直接读原始层的引用 URL,不消费 extract 的产物。
**理由**:② 严格"由①派生",不隐式依赖③的半成品;verify 网络抖动重跑不污染 extract;两段可并行。

## 2. 三档判定(tier)放在 score,不在 verify

`EvidenceRecord` 只记录对 URL 的**客观事实**(可达、域名、来源分类);
`mentioned → cited → verified` 三档标签由 `score` 里的纯函数判定(`TierAssignment`)。

**理由**:三档口径是核心计分规则,放纯层 = 可重算、可黄金测试;v2 升级"earned 页面摘录核验"时只改 score 里一个函数,证据层数据原样复用。verify(只看 URL)本来也不知道"提及"这个文本层事实。

## 3. verified 口径:随核验方式动态分级(F-006 升级)

归属在 extract 完成(owned=域名匹配,earned=引用元数据含清晰名);verified 再补"证据强度"这一环,**口径随实际核验方式动态命名**,在报告与 `ScoreBundle.tier_basis` 永远标注:

| tier_basis | 含义 | verified 判据 |
|---|---|---|
| `verified-page-v2` | 可达 + **页面正文确实提到该品牌**(最强) | reachable 且 brand ∈ `EvidenceRecord.brands_on_page` |
| `verified-reachable-v1` | 可达 = HTTP 状态码(未读正文) | reachable |
| `verified-offline-v1` | 可达 = 离线假设(最弱) | reachable(假设) |

**为什么 page-v2 要求"页面真的提到品牌"**:首次真实抓取发现 deuter.com 软 404("Sales Channel Not Found")页可达但正文根本没提 Deuter,旧"可达即 verified"会误计。`brands_on_page` 由 PageFetchChecker 用与 extract 一致的清晰名匹配产出。
**实测验证**:真实评测站(switchbacktravel/outdoorgearlab 等)的品牌名就在消毒后可见正文里,page-v2 能独立抓到并核验(Osprey/Gregory earned 引用率真实达 1.0)。**注意**:改了核验器输出语义后必须 `verify --refresh` 重抓,否则缓存会复用旧语义的脏证据。

## 3.5 strict / expanded 双模式(F-004)

排名分两口径并列,**绝不混算**:

- **strict(默认排名依据)**:只算品牌清晰名(`BrandMention.counted=True`)。`mention_coverage`。
- **expanded(诊断口径)**:额外含疑似缩写/歧义提及(`counted=False`)。`mention_coverage_expanded` + `ambiguous_mention_count`,报告里单独成段并标注"仅供发现,不作排名依据"。

**为什么不做真模糊匹配**:expanded 只用我们**已检测**的歧义别名(疑似缩写),不引入编辑距离/拼写纠错那类"无中生有造相关性"的匹配——那正是最容易污染测量诚实的地方,留待真有需求时躲在本契约后面加。

## 3.6 排名可信度判语(F-005)

报告 strict 段顶必出一句显式判语:区间重叠 / 采样不足(n_runs<3)/ 存在失败 → **"排名无法区分(not distinguishable)"**,数字仅供观测、不构成确定排序。绿灯才允许谨慎排序(仍仅代表此引擎)。

## 4. 证据层按 URL 去重(核验缓存)

`EvidenceRecord` 以 `normalized_url` 为主键、带 `checked_at`,verify 复用 7 天内同方式的核验结果。
**理由**:同一 URL 跨问题/跨轮反复出现;限速友好,v2 真抓页面时直接省钱("先核 headline、长尾按需")。
可追溯性不受损:URL → 回答 → 指标的归属链由 raw 层 citations + score 的确定性 join 完全重建。
`normalize_url` **保留 www**(规范化后必须仍可抓取);跨 www 变体归并靠 `canonical_domain`。

## 4.5 核验器封装边界(UrlChecker Protocol)

"怎么核验一个 URL"封装在 `contracts/checker.py` 的 `UrlChecker` 接口后面
(与 `ProviderAdapter` 同级的接口预留):`method` 属性诚实标识口径,
`check(url) → CheckOutcome(reachable, method, snippet)` 返回结构化事实。

**理由**:第二步的真页面抓取 + 摘录支持性检查 = **新增一个实现类**
(含 HTML 消毒 + 域名 allow/blocklist),verify/score/extract/storage 全部零改动;
证据缓存按 `check_method` 判断可否复用——换核验口径必然触发重核验,不会静默混用。
v1 的两个实现(OfflineChecker / HttpHeadChecker)都不读网页正文,无 prompt-injection 面。

## 9. web_search 工具版本(2026-06 真实 API 对照实验定论)

第一次接真 key 实测时发现:用 `web_search_20260209`(新版,带"动态过滤"——模型写代码
筛搜索结果)跑出来,**内联引用恒为 0**。挖原始返回确认:14 条搜索结果躺在
`web_search_tool_result` 块里(那是"搜到了什么"),但 31 个 text 块的 `citations`
全是 None,且 5 次过滤代码里有 1 次崩溃(`return_code=1`)。

同一题换 `web_search_20250305`(经典版)对照:**12 条内联引用**,标准
`web_search_result_location`,带 `cited_text/title/url`——我们的契约层一条不漏解析出来。

**定论**:本项目的"被引用(citation)"测量依赖内联引用结构,必须用经典版
`web_search_20250305`。新版的代码化动态过滤会吞掉内联引用,让核心指标失效。
搜索结果(search result)≠ 引用(citation):前者是候选来源,把它冒充成引用会
违背保守计数红线,故 score 只认内联引用归属。

> 经验:这类"离线测试全绿但真实表面不一样"的问题,只有接真网络才暴露得出来——
> 印证了"mock 把链路跑通 + 真实运行校准口径"的两段式价值。

## 5. 可复现的三个机械保证

1. **config_fingerprint**(FrozenConfig 内容 sha256)写入 RunManifest——版本号靠人 bump 会忘,指纹机械暴露"改了配置没 bump"。
2. **纯层不读时钟**:`generated_at`/`now` 一律由 I/O 边缘显式传入;配"同输入跑两遍输出逐字节相同"的确定性测试。
3. **probe 幂等续跑**:raw 层 append 以 `(run_id, question_id, run_index)` 为幂等键,重复写入被跳过——append-only 不破,真实运行崩溃后续跑不重烧预算。

## 6. v1 六指标口径(score 的精确定义)

观测单位 = 一条成功回答(n_obs = 成功回答数)。比例类一律 Wilson 95% 区间;n=0 → `[0,1]` "insufficient",不给假精确。

| 指标 | 定义 | 区间 |
|---|---|---|
| mention_coverage | 含该品牌**清晰名**提及的回答占比(保守计数) | Wilson |
| owned_citation_rate | 含 ≥1 条 **verified owned** 引用的回答占比 | Wilson |
| earned_citation_rate | 含 ≥1 条 **verified earned** 引用的回答占比 | Wilson |
| prominence | 清晰提及的 `1 − 首位/全长` 均值 | **range**(min–max;bootstrap 后期) |
| mention_sov / citation_sov | 品牌(提及/verified)回答数 ÷ 全品牌总数 | Wilson;品牌数 <2 时整体移除 |
| stability | 同一问题跨轮"清晰提及结果一致"的问题占比 | 点值(Jaccard 完整版后期) |

**保守计数的记录约定**(`BrandMention`):清晰命中 `mentioned=True, counted=True`;歧义命中 `mentioned=True, counted=False`(进 extra data,健康灯歧义比例的分子);未提及 `mentioned=False, counted=True`(对"缺席"的有效观测,进覆盖率分母)。

**引用归属约定**(`CitationAttribution`):自有域名 → owned(且**不再**给标题里点到的别家 earned,保守);第三方域名 + 元数据清晰名 → earned(可多品牌各得一条);谁都不沾 → `brand=None` 记录不计分。

## 7. 健康灯(计划书 10.1,总体 = 最差指标)

引用有效性率(可达唯一 URL 占比)/ 歧义比例 / 失败率,对照 `VerifyThresholds` 黄/红线;
另:全部品牌的 mention_coverage 区间存在公共交集 → 红 + `ranking-indicative-only`(无法区分就不排名,这是特性不是 bug——mock demo 均匀采样必然触发它)。

## 8. 其它选型

- **存储 = JSONL + JSON 文件**,无数据库:MVP 数据量小,少一个活动部件;SQLite/Postgres 等趋势库需求出现时再加(§3 依赖红线)。
- **mock = fixture + 确定性模板混合**:fixture 精确覆盖"干净命中/歧义/失败"三情形,模板按 `sha256(prompt_version|question_id|run_index)` 播种,demo 24 题零维护。
- **error_code 是封闭枚举**(timeout/rate_limit/tool_error/parse_error/other)+ 自由文本 error_detail——失败率指标不被碎片字符串污染。
- **report 内部 render(纯)/ write(IO)分离**:Markdown/CSV 可做黄金快照。
- **黄金样本工作流**:`tests/fixtures/golden_e2e_bundle.json` 冻结整个 ScoreBundle;有意改口径时运行 `tools/regen_golden.py` 再生成,人工检查 diff 并在 PR 说明(CLAUDE.md §10)。
- **Windows 注意**:`lint-imports.exe` 可能被应用控制策略拦截,本地用 `python -c "from importlinter.cli import lint_imports; ..."`(pre-commit 已如此配置);CI(Linux)为权威。
