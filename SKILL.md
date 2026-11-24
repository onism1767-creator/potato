---
name: aivis
description: >
  AI web-search 可见度(代理测量;Claude / OpenAI / Gemini / Perplexity / Grok 五家 27 个模型):
  用冻结题库反复探测你选中的每个引擎(web search 开启),以确定性规则统计品牌被提及/被引用的
  频率与结构,每个引擎各出一份带不确定区间、可审计的报告,再并列对比(不混总分)。
  仅观测,不生成"照做就能提升 AI 提及"的建议。
---

# aivis — AI web-search 可见度(代理测量;Claude / OpenAI / Gemini / Perplexity / Grok)

> 薄入口:本文只编排流程,逻辑全在 `aivis` 库 / CLI 里。
> 起草问题的逐桶原则见 [docs/question-drafting-principles.md](docs/question-drafting-principles.md);
> SETUP 完整流程与决策见 [docs/setup-flow-spec.md](docs/setup-flow-spec.md)。

> **不在对话里、想自助?** 跑 `aivis gui` 启动本机网页向导(只监听 127.0.0.1),
> 用图形化 3 步走完同一套 SETUP→run→报告;免费档零成本、不用 key。
> 本 Skill 是同一能力的**对话式**入口(AI 帮你起草、你拍板),适合需要 AI 协作起草时用。

> **原理一句话**:用 24 道固定题(8 发现 / 6 比较 / 6 本地 / 4 防御;前 20 道不点名品牌以免引导
> AI)每题固定问 3 轮(可选 `--sequential`:某题几轮答案一致就少问),再用**确定性规则**(非 AI 判官、保守计数、
> 三档证据 mentioned→cited→verified)计分,每个数字带不确定区间。可复现、可审计。
> **🟥 成本诚实**:本工具开源免费、**作者不收任何费用**;你花的钱是付给你选的 AI 提供商
> (Anthropic / OpenAI / Google / Perplexity / xAI)的 AI 费用(自带 key,各家直接计费),不经作者。
> 全程本机运行、不上传任何数据;每家 key 只发往那一家自己的接口。

## 两种模式

### SETUP(一次性 · 建一条新基线)

触发:用户想测某个品牌在 Claude / ChatGPT / Gemini / Perplexity / Grok 里的可见度,或说 "setup"。照下面 7 步走;
**AI(=对话中的你)只在本模式起草问题/建议竞品,且必须人工确认后冻结**(CLAUDE.md §7.1)。

0. **收集输入**:焦点品牌**全名**(明确请用户给全名,别用简称)+ 常见简称(aliases)+
   歧义简称(进 ambiguous_aliases,只记录不计分);官网域名;**品类/行业描述**(驱动起草);
   locale/region/档位(默认 en-US / US / standard);(可选)用户已知的种子竞品。
   - 🔒 **推荐用户另起一个新对话窗口专做起草** —— 新窗口里焦点品牌没进上下文,起草中立题时更接近"真盲",不易被焦点强项带偏(盲起草是程序性盲,非密码学级)。
1. **建议竞品集**:按品类提候选(名+域名,可用 web_search 落地)→ 用户加/删/确认。
   - 诚实:SOV 只代表"声明的 N 个品牌内的份额",不是绝对市场份额;漏的竞品跑完后由中立题自审计、下一基线补。
2. **盲起草 24 题**(严格 8 发现 + 6 比较 + 6 本地 + 4 防御):
   - 前 20 中立题**只依据品类、绝不点名任何品牌**;4 防御题起草中立双面骨架 + 注入焦点品牌名。
   - 逐桶照 [question-drafting-principles.md](docs/question-drafting-principles.md) 的原则与句式模板。
3. **草稿质检**:`aivis validate --config-dir <dir> --phase draft` → **必须 0 硬错**(此档配比严格);有错你自己改、重起草,不打扰用户。
4. **给用户看 / 改**(用户最终拍板):按桶展示 24 题 + 竞品集,你在旁给意见;用户有完全修改权。
   改完 `aivis validate --phase final`,把结果当意见呈现:🟡 告警(如配比)只提醒;🔴 硬错(中立题点名品牌等红线)大声红色警告并解释危害。
5. **冻结**:
   - `aivis init <name> --focal-name "品牌全名" --focal-domain <域名> [--category "品类"]` 生成焦点模式骨架(24 槽 + 版本号)。
   - 把确认后的 24 题填进 `configs/<name>/questions.yml`、竞品填进 `brands.yml`。
   - 末次 `aivis validate --config-dir configs/<name> --phase final` 复核。
   - 🔴 **红错不拦冻结**:若仍有红错,最后一次大声警告"这样冻结会让测量不诚实,确认是你的决定吗?"——用户确认即冻结,责任归用户(工具是观测器,不是守门人)。
   - **改配置 = 版本号 bump(新基线)**,不许静默混入旧趋势。
6. **交棒 MONITORING**。

### MONITORING(重复 · 跑基线出报告)

只重放冻结配置,不再起草:
- 先 mock 跑通(零成本):`aivis run --config-dir configs/<name>`
- 真实运行(单引擎):`aivis run --config-dir configs/<name> --provider anthropic --model claude-sonnet-5 --yes`
  (`--model` 缺省取该引擎 `--tier` 档默认;`aivis models` 列出目录里全部 30 项——27 个真实模型 + 3 个 mock——与诚实标记)
- 真实运行(多引擎,用户选哪几个就填哪几个):
  `aivis run --config-dir configs/<name> --surfaces anthropic:claude-sonnet-5,openai:gpt-6-luna,gemini:gemini-3.8-flash,perplexity:perplexity/sonar --yes`
  → 每个表面独立 run_id 与报告 + `analysis/compare/<id>/comparison.md|.html` 并列对比 + 账单;
  开跑前会查齐各家密钥环境变量(缺则不发任何调用直接退出)并列出各表面预估与合计
- 真实运行前先 `aivis estimate`:`aivis init` 生成的配置默认 `budget_cap_usd: 5.0`,预估高于它的模型会在开跑前被拦,要先把上限调到不低于预估;
- 可选开关:`--sequential`(自适应采样,稳定的题少问)、`--concurrency N`(同一表面内同时问几道题,默认 4)、`--check head|fetch`(真的去核对被引链接;默认 offline = 假设可达)、`--run-id`(续跑);
- 流程 probe → extract → verify → score → report,产出 Markdown + CSV + HTML(默认离线核验;`--check fetch` 真抓被引页面)。

## 成本闸门(每次真实运行前)

- **开跑前先估**(零成本、不联网):`aivis estimate --config-dir configs/<name> --provider anthropic [--model …]` 或 `--surfaces a:b,c:d` → 逐表面打印"≈N 次调用 ≈ $X(保守上界)"与合计,并对比 `budget_cap_usd`(每个表面各自受限),告诉你哪个会被拦。
- mock(默认)估价为 0,静默通过;
- 真实 provider:估算"≈N 次调用 ≈ $X"并要求人工确认;超 `budget_cap_usd` 开跑前直接阻断;
- **运行中按真实累计花费封顶**:花到 `budget_cap_usd` 就不再发新调用(已发出的照常完成并计费)。这时会就已问到的部分出一份**阶段性报告**(顶部有醒目标注),并以退出码 6 结束;
- **要不要继续由用户决定**:调高 `budget_cap_usd` 后执行 `aivis run --provider … --model … --run-id <id>` 把同一轮接着跑完(只改预算不算换基线);在交互终端里会直接问"要不要调高上限继续"。Skill 里**必须先问用户**,不许替用户决定继续花钱;
- 多表面运行里一个表面到顶不影响其它表面;但只要有表面的**预估**超上限,整批在开跑前就被拦下(一分钱不花)。

## 红线提醒(对 Skill 使用者同样生效)

- 输出一律是"**此引擎此配置下的代理测量**",不是 AI 排名真相;多引擎**只并列、不混总分、不跨引擎排名**;
- 各引擎的诚实标记(不接受温度 / 无区域参数 / 底层会变 / 纯搜索)如实呈现,不替引擎补齐它做不到的事;
- 行动层只给线索(leads),不给保证;
- 核心指标全由确定性规则算;AI 只许在 SETUP 起草(人工确认后冻结),不进测量轨;
- 改配置 = 版本号 bump(新基线),不许静默混入旧趋势。
