# ARCHITECTURE — 为什么这么分层(决策记录)

> 配套阅读:行为约束见 `CLAUDE.md`。
> 本文记录**第一版地基的架构决策与口径定义**,改任何一条 = 改契约级决策,先问人。

## 1. 三层数据 + 流水线

数据分三层、单向流动(CLAUDE.md §1):① 原始观测(append-only)→ ② 核验证据(可重算)→ ③ 分析(完全派生)。

流水线是直线,score 同时读两路输入:

```
probe → ①raw → extract(纯)→ verify(IO)→ ②evidence ─┐
                    └──────────────────────────────────┴→ score(纯)→ report(render 纯 / write IO)
```

**实际做法**:verify 读的是 extract 产出的引用清单(规范化后的 URL + owned/earned 归属),只负责
联网核验并写证据层;score 同时读 extract 的产物和证据层。
**理由**:URL 规范化与归属判定只有一份逻辑(都在 extract),verify 不重复实现;verify 网络抖动
重跑只追加证据层,不动 extract 的产物。
**更正记录(2026-11-23 终审)**:本节原先写的是"菱形——verify 直接读原始层、不消费 extract 的产物",
与代码不符(`verify(extraction, cfg, …)` 的入参就是 extract 的产物),已按代码更正。

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
| prominence | 清晰提及的 `1 − 首位/全长` 均值 | **bootstrap 95% 区间**(固定种子、2000 次重采样;小样本不假精度) |
| mention_sov / citation_sov | 品牌(提及/verified)回答数 ÷ 全品牌总数 | Wilson;品牌数 <2 时整体移除 |
| stability | 同一问题跨轮"清晰提及结果一致"的问题占比 | Wilson(稳定的问题数 ÷ 问题数) |

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

## 10. v0.2 多引擎决策(2026-09,四家 21 个真实表面;目录另含 3 个 mock)

**目录 + 注册表**:`providers/catalog.py` 是纯数据的模型目录(不 import 任何 SDK),`providers/registry.py` 是
provider:model → 适配器的唯一工厂,也是全仓库唯一按引擎名分支的地方;SDK 只在对应分支里惰性 import
(选 mock 不加载付费 SDK,某家 SDK 缺失只影响那一家)。单价与计价逻辑仍在各适配器模块(按 usage 计价,§6.3)。

**表面 = provider × model,各自独立**:每个表面独立 run_id、独立 RunManifest、独立报告、独立受 `budget_cap_usd`
约束;多表面串行跑(`--surfaces`),最后出 `report_compare`(纯渲染)的并列对比页——**只并列、不混总分、
不跨引擎排名**,配置指纹不一致时告警。账单 = 各清单 `cost_usd_actual` 之和。

**清单诚实字段**(RunManifest / ScoreBundle):`temperature_applied` / `region_applied`(该引擎是否真的接受了
钉住的采样温度 / 区域参数;None = 旧记录未记录)、`surface_kind`(answer / search_only)、`cost_usd_actual`。
目录里的 `supports_temperature` / `supports_region` / `pinned` / `surface_kind` 是这些字段的来源。

**各家的坑与定论**(2026-09-22 对照官方文档核实):
- **Claude**:新三代(Sonnet 5 / Opus 5 / Fable 5.1)不接受 temperature,思考常开故 max_tokens 8192;
  `refusal` 停止原因记为失败、**不做服务端 fallback**(换模型作答会让"此表面"失真);经典版
  `web_search_20250305` 在 5.x 上可用(2026-09 真机冒烟已确认,见本节末尾的冒烟结论)。
- **OpenAI**:Responses API + `web_search`;引用 = `url_citation` 标注(起止位置 → cited_text);
  `web_search_call` 项计搜索次数(只有 action=search 算搜索,但所有项都按工具费保守计价);
  `max_tool_calls` = max_uses;`store=False`;推理模型一律不传 temperature;GPT-5.6 系列已从官方总览页
  下架(status=legacy)。
- **Gemini**:grounding 引用 = supports → chunks(未被 support 引用的 chunk 只是搜索结果,不算引用);
  🟥 chunk 的 uri 是 Google 跳转链接,适配器只读跳转头(HEAD、不跟随出 Google 域、不抓正文、最多 3 跳)
  解析真实网址,原链接留在 raw_json.redirects;无 max_uses 类参数(估算按每调用 4 次搜索计,
  grounding $14/千次按全额计);无区域参数(region_applied=False);SAFETY 等 finish_reason → refusal。
- **Perplexity**:Sonar 聊天接口 2026-09-27 停服,走 Agent API:`perplexity/sonar` 显式 model 可钉;
  sonar-pro / sonar-reasoning-pro 按官方迁移表映射为预设 low / medium(底层 OpenAI 模型自动更新,
  `pinned=False`,RawResponse.model 记录每次实际底层模型);引用取 `url_citation`,无标注时按正文
  `[n]` / `[web:n]` 标记映射结果 id(保守);成本优先取 API 自报 `usage.cost.total_cost`。
  **Search API** 是纯搜索表面:answer_text 恒空、每条结果一条引用(cited_text=摘要),提及类指标
  在 score/报告里标"不适用",引用类指标照常;$5/千次固定价;两个表面共用一把 key。

**估算口径**:Anthropic 的每搜索 1.2 万输入 token 是 2026-06 真机校准值;OpenAI 实测同量级(沿用);
Gemini 已按 2026-09-25 真机校准(见下);Perplexity 以 API 自报 cost 为准。

**真机冒烟定论(2026-09-25,每个表面 1 次调用、max_uses=2,合计 ≈ $1.8)**:
- 全部 21 个真实表面请求形状正确;Claude 5.x(Sonnet 5 / Opus 5 / Fable 5.1)**接受经典版 `web_search_20250305`**
  且内联引用正常(16~18 条),Fable 单次 ≈ $0.23、Opus 5 ≈ $0.18、Sonnet 5 ≈ $0.09;
- 模型可以选择不搜索:Haiku 4.5 与 Gemini 3.8 Flash 各有一次"凭常识作答"(searches=0、cites=0)——这是该表面的
  真实行为,如实记录,不补搜;
- OpenAI 的 `url_citation` 标注区间只盖住行内链接标记 "([site](url))",故 cited_text 取标记之前的论述句;
  每次调用 ≈ 2.1~2.2 万输入 token(与 Anthropic 的每搜索 1.2 万口径一致);GPT-6 Astra 单次 ≈ $0.31;
- Gemini 跳转链接 33/33 全部解析成真实网址;grounding 不把搜索结果算进输入 token(prompt 13~1011),
  估算口径已按此校准(基础 1000 + 每搜索 500,每调用按 4 次搜索取上界);
- Perplexity:`perplexity/sonar` 的回答**没有内联引用标注也没有 [n] 标记**(search_results 有 15 条但按保守计数不算引用)
  → 该表面引用类指标可能为 0,目录 blurb 已注明;预设档回答带 `[web:n]` 标记(按结果 id 映射);
  预设档 `temperature` 与 `max_output_tokens` 同给会 400 → 预设档不传温度(supports_temperature=False);
  用量明细键名实测为 `search_web`;Search API 1.1s 返回 10 条,$0.005。

**FatalProviderError 统一在 contracts.adapter**:缺密钥 / 鉴权 / 权限 / 请求格式错误会逐调用复现 → 中止整批;
瞬时错误(限速 / 超时 / 5xx)记成 finished_ok=False 的 RawResponse 继续跑。多表面运行开跑前先查齐各家
密钥环境变量,缺则不发任何调用直接退出。

## 11. Grok(xAI)接入决策(2026-11-19,第五家,6 个表面 → 共 27 个真实表面,目录连 mock 共 30 项)

**不加依赖**:xAI 的 Responses API 与 OpenAI 同形,官方文档示例就用 `openai` SDK 指向 `api.x.ai/v1`。
`providers/grok.py` 复用白名单里已有的 `openai` SDK,只换主机与密钥(`XAI_API_KEY`);建好客户端后把
SDK 从环境读到的 OpenAI 组织 / 项目 ID 清掉(否则会作为请求头发给 x.ai)。pipeline / GUI / CLI 零改动
(GUI 密钥框与模型卡由目录驱动)。

**表面定义**:只开 `web_search`,不开 `x_search`——本项目测"联网搜索回答",X 站内搜索是另一种表面,
要测需单独立项。目录收 6 个:grok-4.20-0309-non-reasoning / -reasoning、4.3、4.5、4.6、4.7
(id 与单价核自 `GET /v1/language-models`);未收 grok-build-0.1(编码模型)与 grok-4.20-multi-agent。
推理强度用各模型默认,不干预。

**诚实标记**:xAI 的 web_search 没有区域参数 → 全部 `supports_region=False`(清单 `region_applied=False`);
temperature 可传且被回显 → `supports_temperature=True`。

**真机定论(6 个表面各 1 次 + 3 次探针,合计 ≈ $0.87)**:
- 行内引用标注(`url_citation`)的区间只盖住标记 `[[n]](url)`,`title` 只是序号 → cited_text 取标记之前
  它所支撑的那句话(连续标记共用一句;片段里不留任何标记 / URL),title 记 None;
- 🟥 **grok-4.7 两次回答都没写行内标记**,此时返回的是一批**零宽标注**(start=end=0、title=url、
  48~53 条)= 代理浏览过的全部来源,不是回答引用的来源 → **保守计数,不算引用**(与 Perplexity
  "未被标记引用的结果不算引用"同口径;原样留在 raw_json)。所以 grok-4.7 的引用类指标可能为 0、
  提及类指标正常,目录 blurb 已注明。4.20 ~ 4.6 有行内引用(7~30 条 / 3~12 个不同网址);
- `max_turns`(= max_uses)卡的是代理轮数不是工具调用数:一轮可并行多搜,`open_page` 同样按
  $5/千次计费,max_turns=3 时计费调用 3~8 次、输入 2.1 万~11.1 万 token。`max_tool_calls` 被 xAI
  忽略(实测传 3 仍调 6 次)→ 不传,不假装有这个上限;`include:["inline_citations"]` 被 400 拒绝;
- 估算第一版照搬"每搜索 1.2 万 token"口径,被真机打穿(6 个表面 5 个超估算)→ 改按轮估:
  每轮 4 万输入 + 3 次计费调用、输出 5000,对全部 8 次真实调用都是上界(实际 = 估算的 22%~62%),
  并有回归测试钉住。代价:demo 配置(72 次调用)下 Grok 各表面估算 ≈ $15~23,超默认
  budget_cap=13 会被开跑前闸门拦下——这是如实结果(grok-4.7 真实单价 × 72 ≈ $14 本就超线);
- 实际花费取 API 自报的 `usage.cost_in_usd_ticks`(1e10 ticks = $1),与"单价表 + 工具费"回退算法
  对账一致;xAI 对坏密钥返 **400**(不是 401)、未知模型返 404,两者都中止整批。

**代码审核后的修正(2026-11-19,自审 + 一次独立审核;全部离线验证,未再花钱):**

- 引用片段断句:原规则要求句末标点后跟空白,中文句号后不跟空格 → 上一句(说别的品牌的)会漏进
  本句的 `cited_text`,被 extract 多记一条 earned(违反保守计数)。改为中文句末标点直接断;
  顺带:标记写在句号之后仍归紧前面那一句、只隔空格的标记共用一句、另起一行或前面只剩标点 → None。
  真机 76 条行内引用的片段改前改后逐条一致(那批回答全是英文、标记紧贴句尾,没踩到);
- 环境请求头:openai SDK 构造时还会读 `OPENAI_CUSTOM_HEADERS` 并把里面的请求头并进每个请求,
  连 `Authorization` 都能顶替(发给 x.ai 的会变成 OpenAI 的凭据)。建客户端后一并清掉
  (SDK 没有公开开关,清的是私有属性 `_custom_headers`,由离线测试钉住);
- `completed` 却没有回答文本、或只有拒答(refusal):原先记成"成功的空回答",现记为失败
  (`other` / `refusal`),不做 fallback;
- 返回形状出乎意料(SDK 不校验 xAI 的返回)导致解析抛错:原先异常冲出 `ask`,这次调用既没留底
  也没记账;现原样留底、记 `parse_error`、花费照记;
- 成本边角:自报 `cost_in_usd_ticks == 0` 不采信(回退按单价算);usage 混进非数字记 0 不抛错。

**审核提出但未改、留待决定的(都不只是 Grok 一家的事):**

- 行内标记 `[[n]](url)` 原样留在 `answer_text` 里,URL 路径里的品牌词原理上会被算成提及
  (OpenAI 适配器的行内链接同理)。真机 6 条回答上去掉标记前后提及结果没有变化,但回答长度
  虚高 10%~26%。改它等于动提及口径,两家要一起定;
- 其它适配器的等待方式:仍是"一个请求等到底" + SDK 默认超时与默认重试 2 次(Grok 已改流式,
  见下)。本机全部真实记录(81 次调用)里没有出现过一次超时或失败,但 Claude demo 72 次调用里
  有 1 次 232 秒(中位 38 秒、90 分位 51 秒;原因查不出——SDK 自动重试不留记录);
- `max_uses=None` 时不传 `max_turns`,估算却按 1 轮(各适配器同一约定);
  估算未计 ≥20 万 token 翻倍(`max_uses ≥ 5` 时估算自身的输入已到 20.2 万);
- ~~OpenAI 适配器同样会把"completed 但无回答文本"记成成功的空回答~~ —— 已在 2026-11-23 解决:空回答五家统一记失败(§13)。

**流式接收(2026-11-19,同日接入;真机 2 次调用验证,共约 $0.10):**

- 为什么:非流式是服务端搜索、读页、推理、写作全做完才回第一个字节,期间连接上没有任何数据,
  中间网络设备(路由、代理、VPN)容易把它当空闲连接掐掉;耗时又长又不稳。xAI 文档对代理式
  工具调用"强烈建议流式",Anthropic 文档对长请求同样建议。后台模式 + 轮询(OpenAI)、延迟完成
  (xAI,仅 chat completions)都要服务端暂存回答,与 `store=False` 冲突,不采用;
- 怎么做:`stream=True`,中途事件只用来保持连接,结果只取终止事件(`response.completed` /
  `incomplete` / `failed`)带的完整 response——真机核实与非流式同形(带位置的引用标注、
  `cost_in_usd_ticks`、计费调用数俱全),解析与计费代码一行没改;
- 超时的含义随之改变:客户端超时 = 120 秒收不到任何数据(真机 grok-4.3:1.5 秒到首个事件、
  全程 23~28 秒、事件间最长空档 5.5 秒),另设单次调用 900 秒总时长兜底;不自动重试
  (原先 900 秒 × SDK 默认重试 2 次,一次 `ask` 最坏挂 45 分钟,重试若已计费还进不了账);
- 如实的局限:120 秒取自一个模型的一次采样,推理更重的模型空档可能更长;流中途断线拿不到
  usage,那次花费只能记 0(服务端可能已计费);实时进度(把"正在搜索/正在写"显示给用户)
  还没做——那要给适配器接口加回调,属于改契约,另议。

## 12. 探测并发决策(2026-11-19)

**为什么做**:慢的主因不是单次调用,而是所有调用排队逐个跑——真实的 Claude demo 72 次调用用了
48.5 分钟(平均每次 41 秒)。单次调用的几十秒花在服务端(联网搜索 + 写几千字),客户端改不了;
能改的是"别排队"。有上限的并发是评测工具的通行做法(promptfoo `--max-concurrency`、
Inspect AI `max_connections`、OpenAI Cookbook 的并行脚本)。

**怎么做(只改 probe 这一层;契约、适配器、纯层零改动)**:

- `run_probe(..., concurrency=N)`;CLI `--concurrency`,默认 4,`1` = 与改动前完全一致的逐个串行;
- 并发的单位是"题":同一时刻最多 N 次调用在途,且**同一题只有一次在途**。每题的轮次仍按
  0,1,2… 顺序发出和落盘,所以续跑("已问轮次是连续前缀")和序贯采样("这题稳了就停")的逻辑原样成立;
- 调度、写原始层、累计花费、进度回调全在主线程;工作线程只做 `adapter.ask` 这一次 I/O。
  原始层仍是单写者、只追加,不需要加锁;
- 落盘顺序变成"谁先回来谁先写"(题与题之间交错)。核实过不影响结果:把 mock 与真实 Claude
  的 72 条记录打乱顺序重跑 extract + score,产物逐字段相同;并发 1/2/4/8 路的 extract 产物
  也与串行逐字段相同。

**如实的代价与局限**:

- 预算封顶:达到上限后不再发新调用,但已发出的调用收不回、照常保存并计费——最多比串行多花
  N−1 次调用的钱(有测试钉住);
- 出现不可恢复错误(坏密钥等)时:不再发新调用,等其余在途调用回来并保存后再抛错——
  首批的 N 次调用都会各自撞一次错,而不是只撞 1 次;
- 提速幅度只做了离线测量(每次调用固定耗时的假引擎:4 路 ×3.9、8 路 ×7.7);真实引擎上
  还受各家限速制约,**没有做真机并发测试**。被限速时表现为失败数变多(Grok 不自动重试,
  其它家 SDK 会重试 2 次),调小 `--concurrency` 即可;
- 适配器实例被多个线程同时调用:各家 SDK 客户端基于 httpx,按文档是线程安全的。
  **更正(2026-11-23)**:这里原先把"惰性建客户端可能多建一个、后者覆盖前者"写成良性竞争、未加锁。
  对 Gemini 它不是良性的——被覆盖的 Client 一被回收,SDK 就关掉仍在使用的连接池,首批调用即整批中止
  (终审用真 SDK + 本机假服务器在 Windows 上复现)。现在五家建客户端都加了锁,只建一个;
- 表面之间仍是串行(`--surfaces` 逐个跑);并发数没有记进 RunManifest(那要改契约,另议)。

## 13. 终审修正与已知待办(2026-11-23,v0.3)

发布前做了一次全项目独立代码审查(内核与探测 / 五家适配器 / 向导·命令行·核验安全 / 文档与打包四路,
各自通读源码并离线复现)。没有发现密钥泄露或 XSS;查出的是"花了钱的数据会丢""默认并发下跑不起来"
"文案与实际不符"三类。本节记下**改了什么、为什么**,以及**查出但这次没改的**。

### 13.1 预算到顶:看得到已跑的部分,继续与否由用户决定(用户拍板)

- **原问题**:运行中花到 `budget_cap_usd` 会停下并提示"调高预算后续跑",但预算被算进了配置锁的指纹,
  调高预算就被 `RunLockError` 拒绝;这轮又没有清单,报告也出不了——已花钱的数据两头都用不上。
- **决策**:`budget_cap_usd` 只决定"花到多少停",不改变问什么、怎么问,**不算测量基线**。
  契约新增 `FrozenConfig.baseline_fingerprint()`(与 `fingerprint()` 相同但不含预算),配置锁比对它;
  旧锁存的是全量指纹,预算没变时仍然认得。清单里的 `config_fingerprint` 仍是全量指纹(如实记下跑完时的预算)。
  除预算外任何字段改动仍然换基线、仍被拒绝。
- **阶段性报告**:到顶时 probe 抛 `BudgetStop`(带 run_id / 已完成数 / 花费)。`run` 命令用
  `snapshot_manifest()` 现算一份清单——**不写入原始层**(原始层仍只在整轮跑完时由 probe 写一次清单)——
  就已保存的数据走完 extract → verify → score → report。`score(partial=True)` 只加标记
  `partial-run:budget-cap`、把健康灯压到至少为黄,**不改任何指标数字**;报告顶部有醒目横幅:
  只覆盖已问到的题,排在后面的题可能一次都没问,不能当作完整基线。
- **谁来决定继续**:永远是用户。交互终端里直接问(输入更高的上限 = 接着跑,回车 = 停);非交互时打印
  `BUDGET_STOP <表面> <run_id> <已完成>/<总数> <花费>` 机器行并以退出码 6 结束;向导里显示
  "打开阶段性报告"和"调高上限并接着跑"(沿用磁盘上已冻结的配置、只改预算)。
- **多表面**:每个表面各自受限,一个表面到顶不再连累其它表面。但只要有表面的**预估**超上限,
  整批在开跑前就被拦下——以前是前面的表面花完钱、轮到它才中止,既没对比页也没账单。

### 13.2 其它修正

- **探测**:主线程被打断(Ctrl+C / 回调或落盘抛错)时,在途调用的结果照常落盘记账后再抛原异常
  (以前丢弃,续跑要再付一次钱);序贯采样只把**成功**的回答当观测(以前失败的空回答被当成"谁都没提到":
  全失败在 min_runs 就判稳定,一次瞬时失败会逼着问到上限);配置锁改到成本闸门通过后才写。
- **原始层 / 证据层**:写到一半被杀留下的半截行,读取时跳过并告警,追加前先补换行隔开
  (以前一行残渣让整个数据目录都读不出来)。仍然只追加、不改写。
- **空回答五家统一记失败**(用户确认按 bug 修):没有回答正文不是一次"所有品牌都没被提到"的观测。
  以前只有 Grok 记失败,其余四家记成功,既压低覆盖率、又让并列对比的失败率不可比。花费照记。
  本机留存的 79 条真实回答里没有出现过空回答,这是用假响应复现的潜在问题。
- **解析兜底**:四家老适配器在解析阶段出异常时原样留底、记 `parse_error`、花费照记(与 Grok 同做法)。
- **端点钉死**:五家客户端都显式指向官方地址,环境里的 `*_BASE_URL`(中转站)不再能把密钥带走;
  Gemini 显式使用 `GEMINI_API_KEY`。钉的地址由测试对照各家 SDK 的默认值。
- **Gemini**:建客户端加锁(见 §12 的更正);设 10 分钟超时(SDK 默认不设,连接被掐掉后调用永不返回);
  传输层错误记为失败而不是崩掉整批。
- **抽取 / 打分 / 报告**:畸形被引 URL 不再让 extract 崩溃;配置里的域名容忍整条网址;
  纯搜索表面不再因提及覆盖全为 0 被判红;HTML 报告在排名无法区分时不再写"第一名 / 领先几个点",
  离线核验时明说可达性是假设值,序贯采样不再写成"固定 N 轮",删掉写死的"Anywhere else: 0"。
- **向导**:意外异常回固定文案的 500(以前直接断开连接,页面一直卡住);Windows 上不再允许两个实例
  并绑同一端口;真实模式的估算按钮不再回 mock 的 $0;`gui/api` 不再按引擎名分支(§4.3)。
- **核验(仅 --check head/fetch)**:`localhost.`、十进制/八进制/十六进制 IP 写法不再能绕过内网拦截;
  HTML 解析与正文读取都加了总时限;未知编码回退 utf-8。

### 13.3 已知待办(审查查出、这次没有改)

按"会不会让用户吃亏"排序。前三条需要改契约或改口径,要先由人拍板。

1. **配置契约没有收紧**:两道题 id 重复或两个品牌重名不报错(会重复付费、漏问、份额算错);
   键名拼错(如 `max_use`)被静默忽略,成本估算随之失准。修法是给 `FrozenConfig` 加唯一性校验和
   `extra="forbid"`——改契约,未拍板。
2. **开跑前的估算不是严格上界**:Claude 估输出 2000 token 而适配器允许 4096/8192;Gemini 按每次调用
   4 次搜索 + 4000 输出估,而搜索次数封不了顶。对照真机冒烟,余量只有 2%~10%。有运行中上限兜底,
   钱不会失控,但运行可能中途到顶(现在会出阶段性报告并可续跑)。把估算调高会让更多模型在默认上限下
   被开跑前拦下,连带改文档里的金额,未动。
3. **品牌名匹配对排版字符敏感**:名字写直引号 `Arc'teryx`、文本是弯引号 `Arc’teryx` 时不命中
   (不同引擎用弯引号的比例不同,带撇号品牌的漏计会随引擎而变);词边界只认 ASCII,`Citro` 会命中
   `Citroën`。修它等于改提及口径,要同步更新黄金样本,未动。
4. **score / extract 不核对传入配置与清单的指纹**:探测后改了 brands.yml 再单独重跑
   `aivis score --run-id X`,结果按新配置算、却标着旧版本号。`aivis run` 一条龙和向导不受影响。
5. **向导永远是离线核验**(不抓被引网页):界面和报告现在都如实写明"已核实是假设值"。
   要不要让向导支持真抓取是产品决策(会明显变慢)。
6. **核验的内网拦截不做 DNS 解析**:一个 A 记录指向内网地址的普通域名、或 DNS rebinding 拦不住。
   只影响 `--check head/fetch`,且攻击者拿不到回显。
7. **证据缓存不看品牌集**:page-fetch 口径下,7 天内新增的品牌在已缓存的页面上不会被算作 verified,
   需要 `verify --refresh`。
8. **多表面运行中途遇到不可恢复错误**(如第二家密钥错):整批以非零退出,已完成表面的报告都在
   (向导里也能打开),但没有对比页和账单。
9. `aivis init` 生成的"待填"占位题能通过 `validate`,`run` 也不拦,可能被直接拿去付费提问。
10. 三处需要用真机记录核对的疑点:Grok 流里官方形状的 error 事件被记成"超时"(原因丢失);
    Claude 的 `max_uses_exceeded` 错误块让整条回答记失败是否过严;Perplexity 多个 `search_results`
    若各自从 1 编号,`[web:n]` 可能映射到错的网址。
11. 并发数没有记进 RunManifest;`n_runs=1` 时稳定性恒为 1.0(测试钉住的有意口径)。
12. 本地 pre-commit 的 gitleaks(v8.21.2)与 CI 用的版本没有对齐——本地过不代表 CI 过。
    已知的那条误报已加进 `.gitleaksignore`。
13. `configcheck.Violation`、`templater.Segment` 等跨模块传递的冻结 dataclass 定义在 `contracts/` 之外,
    按 CLAUDE.md §2 的字面算越线(它们是带类型的冻结对象,不是临时 dict)。
