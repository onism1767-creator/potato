# 起草诚实原则 —— 供人工审核(Drafting Honesty Principles · for Review)

> **给审核者**:读完本文,你应当能回答四个问题——
> **① 我们怎么防偏? ② prompt 是否真的防偏? ③ 机器能查什么? ④ 人必须查什么?**
> 请同时挑战:这套原则站得住吗?我们的**实现状态**有没有被夸大?有没有隐性偏置或夸大其词?

> **文档关系(三者状态须一致)**:
> - **本文** = 面向审核者的"诚实说明 + prompt + 审核工具包"。
> - [question-drafting-principles.md](question-drafting-principles.md) = 内部规则书(AI 指令 + 代码护栏)。
> - [setup-flow-spec.md](setup-flow-spec.md) = 当前 Skill 版实现流程;[gui-phase2-design.md](gui-phase2-design.md) = 目标 GUI 设计。
> - 凡涉及"已实现 / 待实现",一律以本文 **§0.1 实现状态声明**为准。

---

## 0. 30 秒背景(为什么"起草"这一步的诚实是命门)

aivis 测的是:**一个品牌在 Claude(开 web 搜索)的回答里,被提及 / 被引用的频率**。
方法是反复用一批**冻结的问题**去问 AI,收集回答 + 引用,再用确定性规则打分。

> **如果问题本身偏向某个品牌,等于"自己把答案塞进问题里"——测出来的可见度就是假的。**
> 所以"起草问题"这一步的中立,是整个测量诚实的**源头**。本文就是为守住这个源头。

**24 题配方** = 发现 8 + 比较 6 + 本地/场景 6 + 品牌防御 4。
- **前 20 题(发现/比较/本地)= 中立题**:绝不点名任何品牌。测"AI 在用户随便问时,**主动**提到谁"。
- **后 4 题 = 防御题**:**故意**点名用户自己的品牌(焦点品牌)。测"AI 被**直接问到你**时怎么说你"。

---

## 0.1 实现状态声明(🟥 审核者必读 —— 防止把"计划"当"已实现")

| 能力 | **Claude Skill 对话式 SETUP(已实现)** | **GUI 程序化起草(已实现,2026-06)** | 未来增强 |
|---|---|---|---|
| 起草者 | 对话中的 Claude 本人,照规则书 + §4 模板手动起草 | 程序化 API 调用(`providers/drafter.py`,已实现) | — |
| 盲法强度 | **程序性隔离盲**:推荐用户**另起新窗口**起草中立题(焦点不在上下文);非强制 | 中立题用**独立、不含焦点名的 API 调用**(机制上更接近隔离盲) | 独立子 agent / 多模型交叉 |
| 已写的新代码 | 仅 `aivis init`(脚手架)+ `aivis validate`(纯函数校验闸) | + `templater.py`(免费模板,已写)+ `drafter.py`(AI,已写) | — |
| 起草后纠错 | **由 Claude 据 validate 结果自己重起草**(对话内,无自动编排) | **有界自动重试**(代码编排,每步最多 3 次)后交人 | — |
| 冻结把关 | `validate --phase draft/final` + 人最终拍板 | 同左,接进向导 | 把未解决红错写入 RunManifest(触契约,需单独讨论) |

> **注(2026-11-23 更正)**:上表第三列描述的 GUI 程序化起草已经落地。代码里实际发送的提示词比本文 §4 **精简**(只要求最小 JSON,没有 `local_rationale` / `self_check` 字段,向导也不回传原始输出),以 `providers/drafter.py` 为准;§4 保留作起草思路与人工审核清单。

> **🟥 因此**:本文 §4 的三段 prompt 是**目标 GUI 将采用的 prompt**;**用于当前 Skill 版时,它们是"人工执行模板"**(对话中的 Claude 照此起草)。文中凡说"独立 API 调用",均指**目标 GUI**,当前**尚未实现**。

---

## 1. 最高红线(违反即测量无效)🟥

1. **反偏置**:中立 20 题**绝不出现任何品牌名**(焦点、竞品都不许)。品牌靠规则从**回答**里检测,**不写进问题**。
2. **隔离盲起草**(注意措辞):起草中立题时**不向起草者提供焦点品牌名**。当前 Skill 版靠"推荐新窗口"做程序性隔离;目标 GUI 用独立调用。**这是程序性隔离,不是密码学级盲法——模型仍可能有训练先验**(见 §9.1)。
3. **不夹带独家卖点**:不把"只有某一家满足的特征"伪装成中立提问(变相引导,见 §3.1 案例)。
4. **不预设答案**:不用 "why is X best" / "the best … is" 这类预置结论的诱导句式。
5. **防御题双面中立**:framing 必须**同时邀请优点和缺点**,不许单边引导;**只许提焦点品牌,不许点名竞品**。
6. **人最终拍板,但工具不静默纵容**:AI 只**起草**,用户可改/删/改写,最终冻结由人负责。破红线时**保留红色警告、要求用户显式确认、并建议把未解决红错写入审计记录**(目标:写进 RunManifest)——**不替用户拒绝,但也绝不无声放过**。
7. **一次性冻结、绝不进测量轨**:起草是 SETUP 的**一次性**动作,冻结成基线;**绝不每次运行重生成**(否则基线断裂、不可复现)。AI 不参与任何计分。

---

## 2. 通用原则(全部 24 题)

1. **真实买家口吻**:像真在做决策的人随口问,不是营销文案/关键词堆砌。
2. **一题一意图**:不写"既问 A 又问 B"的复合题。
3. **桶内多样性**:同桶覆盖不同角度,不许换皮重复。
4. **守在品类内**:只问用户给的品类,不漂移。

---

## 3. 逐桶目的与审核要点

| 桶 | 题数 | 目的 | 审核者最该问自己 |
|---|---|---|---|
| **发现 Discovery** | 8 | 漏斗顶端随便问,AI **主动**提谁 | 是否覆盖入门↔资深、价格↔高端、规格↔服务的光谱?有没有两题在问同一件事? |
| **比较 Comparison** | 6 | AI 怎么**横向比**各家 | 6 个比较轴是否**真有多家可比**?有没有一题的轴暗指某家独有强项? |
| **本地/场景 Local** | 6 | 具体场景下 AI 提谁(**最危险的一桶**) | **每个场景**:这是该品类**真实常见**的吗?还是被裁剪成只契合某家利基?(要求起草者逐条给理由,见 §4) |
| **品牌防御 Defense** | 4 | 被直接问焦点品牌时 AI 怎么说 | 4 题是否都留了"也可能负面"的空间?有没有偷偷夸? |

### 3.1 隐性偏置案例(审核者最需要的对照)

机器查不出、最会藏的偏置长这样——**字面中立,实则只有一家满足**:

| 桶 | ❌ 隐性偏置(过窄/暗指一家) | ✅ 中立(多家可比) |
|---|---|---|
| 比较(CRM) | "best CRM with built-in **Slack-first pipeline automation**" | "how do top CRM tools compare on workflow automation?" |
| 本地(背包) | "best ultralight **framed** pack **under 2 lb** with **bear-canister fit**" | "what backpacks are best for multi-day backcountry trips?" |
| 发现(咖啡) | "best **single-origin subscription** coffee with **same-day roast shipping**" | "what are the best specialty coffee brands for home brewing?" |

> 共同特征:叠加了一串**只有目标品牌恰好满足**的限定词。审核本地/比较桶时,逐条问:"**去掉品牌,真有 3+ 家能进这个问题的候选名单吗?**"答否 = 偏。

---

## 4. Prompt(目标 GUI 将采用;当前 Skill 版作为人工执行模板)

> **🟥 执行顺序(决定"盲"是不是真的)**:必须 **① 先在不含焦点/竞品的上下文里起草中立 20 题 → ② 再做竞品建议 → ③ 再起草防御题**。
> 若顺序颠倒(先收集焦点/竞品再起草中立题),"隔离盲"就只剩口头承诺。
> **竞品建议虽知道焦点(为排除自己),但其输出只用于"后续检测 + 防御题校验",绝不回流改写中立题**(防焦点反向泄漏)。
> 所有调用要求:**输出必须是合法 JSON(valid JSON),不许 markdown 代码围栏、不许注释、不许多余文字**。导入时只取问题数组,`self_check` / `rationale` 仅供人工审核。

### ① 中立 20 题 · 隔离盲(只给品类,绝不给焦点名)
```
You are drafting neutral market-research questions about a product category —
the kind a real buyer asks a search assistant. You are deliberately NOT given any
specific brand. Do not ask for one.

Category: {category}
Language/locale: {locale}

Draft EXACTLY: discovery 8, comparison 6, local 6.
- discovery: open "what/which is best/recommended for…", spread across buyer
  sub-personas (beginner, experienced/pro, budget, premium, spec/size,
  service/warranty, usage scenario, durability). No two ask the same thing.
- comparison: "how do top brands compare on X" — each a DIFFERENT axis that
  MULTIPLE brands genuinely compete on.
- local: six DISTINCT, realistic in-category scenarios real buyers face.

HARD RULES:
- NEVER name any brand, company, or product line.
- Do not stack qualifiers that only ONE brand could satisfy (covert bias).
- Real buyer voice; one intent per question; no presupposed answer ("why is X best").
- Stay strictly inside the category "{category}".

Return ONLY valid JSON, no markdown, no comments, exactly this shape:
{
  "discovery":  ["string", "string", "string", "string", "string", "string", "string", "string"],
  "comparison": ["string", "string", "string", "string", "string", "string"],
  "local":      ["string", "string", "string", "string", "string", "string"],
  "local_rationale": ["string x6: for each local question, ONE line on why this
                       scenario is common in {category} and NOT tailored to one brand"],
  "self_check": {
    "no_brand_names": true,
    "no_repeated_intent": true,
    "no_one_brand_only_feature": true,
    "counts": {"discovery": 8, "comparison": 6, "local": 6}
  }
}
```

### ② 防御 4 题 · 焦点感知(注入焦点名;在中立题之后)
```
Draft EXACTLY 4 brand-defense questions about ONE specific brand.

Brand: {focal_name}
Category: {category}
Language/locale: {locale}

Cover: overall quality · how it compares to peers · community/user opinion ·
value for money.

HARD RULES:
- Each question names ONLY {focal_name}. NEVER name a competitor.
- Framing TWO-SIDED and neutral — explicitly invite BOTH strengths and weaknesses.
  Never one-sided ("why is X best" / "is X bad").
- Real buyer voice; one intent each.

Return ONLY valid JSON, no markdown, no comments:
{
  "brand_defense": ["string", "string", "string", "string"],
  "self_check": {
    "names_only_focal": true,
    "no_competitor_named": true,
    "two_sided_framing": true
  }
}
```

### ③ 竞品建议 · web_search(焦点感知仅为排除自己;输出不回流中立题)
```
Using web search, list the most prominent REAL competitor brands in the
"{category}" category, EXCLUDING {focal_name} (the user's own brand).
Give each brand's common name and primary official website domain.
Only real, well-known brands in this category. Do not invent.

Return ONLY valid JSON, no markdown, no comments (at most {k} items):
{
  "competitors": [
    {"name": "string", "domain": "example.com"}
  ]
}
```

---

## 5. 双层把关 —— 机器查明显错,人查隐性偏置

### 5.1 机器能查什么(纯函数 `validate_config`,起草后自动跑)
- 🔴 **硬错**:中立题含**任何已声明品牌的清晰名(name+aliases)**(反偏置)· 配比不是 8/6/6/4(draft 阶段)· 防御题没点名焦点 / 点名了竞品 · 空题。
- 🟡 **告警**:重复题 · 命中诱导句式黑名单(`why is … best` / `the best … is`)。

### 5.2 机器**查不出**什么(🟥 必须人查 —— 这是审核的核心价值)
- **场景是否偷偷偏向某家**(本地/比较桶的隐性裁剪,见 §3.1)——机器只认**已声明的品牌字符串**,认不出"一串只有某家满足的限定词"。
- **未声明品牌/产品线**的暗示(机器只查你填进 brands 的名字)。
- **比较轴是否真多家可比**、**防御题语气是否偷偷夸**。

> 一句话:`validate_config` 挡的是"明着点名 + 配比/空题/部分诱导句式";**"暗着偏"必须靠人**。

---

## 6. 审核矩阵(每条原则:prompt 怎么执行 · 机器怎么查 · 人怎么查 · 剩余风险)

| 原则 | Prompt 如何执行 | 机器如何检查 | 人如何检查 | 剩余风险 |
|---|---|---|---|---|
| 中立题不点名品牌 | prompt 禁止 brand/company/product line | `validate_config` 查**已声明**品牌清晰名 | 查**未声明**品牌/产品线、行业暗示 | AI 仍可能写行业暗示词 |
| 不夹带独家卖点 | prompt 禁止"only one brand could satisfy" | **暂无可靠机器检查** | reviewer 用 §3.1"去掉品牌还有 3+ 家吗"判比较轴/场景 | 需要行业知识 |
| 隔离盲 | 中立调用上下文不含焦点名;顺序①先于②③ | 无(机制保证) | 核对调用顺序 + 中立题无焦点名 | 程序性隔离≠密码学盲,有训练先验 |
| 本地场景真实 | prompt 要 6 个真实场景 + 逐条 rationale | 无 | 读 `local_rationale`,判场景是否常见、是否裁剪 | 最易藏偏,最依赖人 |
| 防御题双面 | prompt 要 strengths + weaknesses | 查焦点/竞品名(点名对不对) | 判 framing 是否单边、有没有偷夸 | 语气仍可能偏夸 |
| 配比 8/6/6/4 | prompt 指定数量 + self_check.counts | `validate_config` 硬错(draft) | 复核计数 | 低 |

---

## 7. 审核者交付物清单(让 reviewer 拿证据复核,而非只读文档)

审核一批起草时,应能拿到以下**审核包**:

1. 用户填写的 **`category`**(及 locale/region)。
2. 实际使用的**三段 prompt 原文**(本文 §4,或当次替换变量后的版本)。
3. **AI 原始输出**(三次调用的原始 JSON,含 `self_check` / `local_rationale`)。
4. 冻结后的 **`questions.yml` + `brands.yml`**。
5. **`aivis validate --phase draft` 与 `--phase final` 的输出**(违规清单 + 退出码)。
6. **用户修改记录**(AI 草稿 → 用户改动 → 最终冻结的 diff)。

> 有了 1–6,审核者可独立复核:"中立题真没品牌名吗?顺序对吗?用户改了什么?validate 报了什么、解决了吗?"

---

## 8. 通过 / 失败判据(审核者据此下结论)

### ✅ 可接受(全部满足)
- 20 个中立题**无任何品牌名**(已声明 + 未声明,人也核过);
- 配比 **8/6/6/4** 正确;
- 每桶**覆盖多角度**(发现铺开子人群、比较轴各异、本地场景互不相同且真实);
- 防御 4 题**双面**、只提焦点、不提竞品;
- **审核包 §7 齐全**,validate 的红错全部已解决或有显式记录的用户确认。

### ❌ 应判不合格(命中任一)
- 中立题出现**任何**品牌名 / 产品线 / 强行业暗示;
- 本地或比较题**过窄**("去掉品牌不足 3 家可比",见 §3.1);
- 比较轴**只有一家**能满足;
- 防御题**单边夸赞**或点名竞品;
- **重复题 > 2 条**,或配比不符且无合理说明;
- 审核包缺失关键证据(拿不到 prompt / 原始输出 / validate 结果)。

---

## 9. 诚实披露的已知局限(不藏)

1. **"盲"是程序性隔离,不是密码学级**:即使中立调用不含焦点名,**同一模型权重仍可能带训练先验**。我们能做的只是:机制上不喂焦点名 + 顺序保证 + 机器查中立题无品牌名 + 人审场景真实性 + (未来)独立子 agent / 多模型交叉。
2. **免费档(模板起草)的本地题偏泛**:不调 AI 时,场景题只能用通用场景(模板吃不到行业语境),质量弱于 AI 档——GUI 会把这 6 题标黄提示用户手改。
3. **`self_check` 不能替代校验器**:它是模型**自述**,可能自评不准;只作人工审核的辅助线索,真把关靠 `validate_config` + 人。
4. **保守计数会低估**:只有清晰品牌名计分,缩写/歧义只记录不计分(宁可少算不可错算)。
5. **代理测量**:结果只代表"此引擎 × 此配置 × 此时间窗口",不是"AI 排名真相"。
