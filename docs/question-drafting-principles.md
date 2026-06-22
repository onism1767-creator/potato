# 问题起草原则 —— AI 按行业现起草 24 题的规则书

> 配套 [setup-mode-design.md](setup-mode-design.md)。本文是 SETUP 模式 B 做法的**核心规则**,
> 一处三用:**① AI 起草时的指令** · **② 用户审核时的清单** · **③ 机器校验闸的依据**。
> 配方来自计划书第 7 章:**24 题 = 发现 8 + 比较 6 + 本地/场景 6 + 品牌防御 4**。
> 语言/区域跟随冻结配置(如 en-US);本文示例用英文,因为问题本身是英文。

---

## 0. 起草输入与输出

**给 AI 的输入**:品类/行业描述(用户填,如 "hiking backpacks" / "CRM software" / "specialty coffee beans")、locale/region。
- 🔒 **发现/比较/本地三桶起草时,不告诉 AI 焦点品牌是谁(盲起草)** → 它无法偏向你。
- 品牌防御桶单独起草句式骨架,焦点品牌名最后注入。

**输出**:24 条问题,严格 8/6/6/4,每条标 `intent`。

**两个检查点(都过机器校验闸 §6)**:
1. **AI 起草完 → 查一次(phase=draft,配比严格)**:AI 必须严格交出 8/6/6/4,不合直接打回重起草。
2. **用户审核/编辑/拍板后 → 再查一次并给意见(phase=final,配比仅提醒)**:用户有最终修改权,改完即使不是 8/6/6/4 也只**提醒**、不拦;但**反偏置等红线两阶段都硬错**。

流程:AI 盲起草 →(draft 查)→ 用户审 / 改 / 拍板 →(final 查 + 给意见)→ 冻结。

---

## 1. 通用原则(全部 24 题都适用)

1. **真实买家口吻**:像一个真在做决策的用户随口问,不是营销文案、不是关键词堆砌。
2. **一题一意图**:不写"既要 A 又问 B"的复合问题,否则提及/引用归因会糊。
3. **不预设答案**:不用"为什么 X 最好""哪个一定是第一"这类预置结论的引导句式。
4. **桶内多样性**:同一桶的几道题要覆盖**不同角度/子需求**,不许换皮重复同一个问题。
5. **守在品类内**:只问用户给的品类,不漂移到别的行业。
6. **🟥 反偏置(发现/比较/本地三桶 = 前 20 题):绝不点名任何品牌**(焦点、竞品都不许出现);品牌是靠 `extract` 在回答里**检测**出来的,不是写进问题里的。
7. **不夹带只有某一家满足的特征**:别把"某品牌独有的卖点"伪装成中立提问(那是变相引导)。

---

## 2. 发现 Discovery(8 题 · 不点名)

**目的**:测"用户在漏斗顶端随便问,AI 会**主动**提到谁"。最接近真实曝光。

**原则**:开放式 "what/which is best/recommended for …",覆盖**一组不同买家子人群**,8 题要在下列维度上铺开,别都问同一类:
- 入门新手 · 资深老手 · 价格/超值 · 高端/premium · 具体规格或尺寸 · 服务/保修 · 使用频率/场景 · 耐用/品质

**句式模板(抽象,AI 据品类填空)**:
- `What are the best {category} for {primary use case}?`
- `Which {category} brands are most recommended by {expert persona}?`
- `What {category} should I buy for {beginner scenario}?`
- `What are the most {generic attribute, e.g. durable} {category} right now?`
- `Which {category} do {serious-user persona} actually use?`
- `What is a good {spec/size/tier} {category} for {scenario}?`
- `Which {category} brands offer the best {service dimension}?`
- `What are the best value {category} under {price point}?`

**🟥 红线**:任何品牌名都不许出现。**审核要点**:8 题是否覆盖了入门↔资深、价格↔高端、规格↔服务的光谱,有没有两题其实在问同一件事。

---

## 3. 比较 Comparison(6 题 · 不点名)

**目的**:测"AI 怎么**横向比**各家",以及比的时候提到谁。

**原则**:问"头部/主流品牌在某维度上怎么比",**每题换一个比较轴**,轴必须是**多家都在竞争**的通用维度,不是某一家独有的隐藏卖点。

**句式模板**:
- `How do the top {category} brands compare on {dimension}?`
- `Which brands have the best {sub-system/feature}, and how do they differ?`
- `Comparing popular {category}, which ones fit {specific need} best?`
- `Which {category} brands are {attribute A} without sacrificing {attribute B}?`  ← 权衡式
- `How do {mainstream segment} compare to {niche/challenger segment}?`
- `Which brands offer the best {sub-segment-specific, e.g. women's-fit} options?`

**🟥 红线**:不点名品牌;比较轴不许是"只有焦点品牌满足"的特化维度(变相引导)。**审核要点**:6 个比较轴是否真有多家可比、有没有一题暗指了焦点品牌。

---

## 4. 本地 / 场景 Local(6 题 · 不点名)

**目的**:测"在**具体真实场景**下 AI 提谁"。**这桶最吃行业语境 —— 也正是必须 AI 按行业现起草的原因**(背包的场景 ≠ CRM 的场景 ≠ 咖啡的场景)。

**原则**:6 个**互不相同、品类内真实常见**的使用场景/边缘条件,问"这个场景下该用/买什么"。

**句式模板**:
- `What {category} should I use for {specific named scenario/context}?`
- `Best {category} for {environment/condition}?`
- `What do people recommend for {scenario with a special requirement}?`
- `Which {category} works well for {edge-case condition}?`
- `What {category} is best for {dual / combined use case}?`
- `Recommended {category} for {advanced / niche scenario}?`

**🟥 红线**:不点名品牌;**场景不许被"裁剪"成只有焦点品牌的强项**(例如硬造一个只契合某家利基的怪场景)。**审核要点(最关键的一桶)**:逐条问自己"这个场景是这个行业里真实常见的吗?还是被定制来偏向某家?"——这是隐性引导最容易藏的地方。

---

## 5. 品牌防御 Brand Defense(4 题 · **故意点名焦点品牌**)

**目的**:测"当 AI 被直接问到**你这个牌子**时,它怎么描述你"。

**原则**:AI 起草**中立、双面**的句式骨架,再把焦点品牌名注入 `{focal}`。framing 必须**同时邀请优点和缺点**,不许单边引导(既不许"为什么 {X} 最好",也不许"{X} 是不是很差")。覆盖:总体品质 · 与同行比 · 社区口碑 · 性价比。

**句式模板(骨架 + 注入焦点名)**:
- `Is {focal} a good {category} brand? What are its strengths and weaknesses?`
- `How does {focal} compare to other {category} brands in terms of {dimension}?`
- `What do {users/community} think about {focal} {products}?`
- `Are {focal} {products} worth it for {use case}?`

**🟥 红线**:只许出现**焦点品牌名**,不许点名竞品;framing 必须双面中立。**审核要点**:4 题是否都给了"也可能负面"的空间,有没有偷偷夸。

---

## 6. 机器校验闸(代码强制,非人;挡明显错后再交人审)

实现:纯函数 `validate_config(cfg, phase) -> list[Violation]`(`src/aivis/configcheck.py`)+ 薄 CLI `aivis validate --config-dir X --phase {draft|final}`。每条违规带**严重度**(error 拦 / warn 提醒)。**mode-aware**:focal 模式(focal_domain 非空)与 peer 模式(为空,如 demo)规则不同。

**🔴 硬错(error,两阶段都拦)**:
1. **反偏置**:发现/比较/本地题(中立题)**含任何已声明品牌的清晰名(name+aliases)** → 会诱导 AI、污染测量。(歧义别名不查,避免误伤常见词)
2. **focal 模式**:`is_focal=true` 恰好 1 个,且其 domains 含 `focal_domain`;品牌防御题**必须点名焦点品牌**、**不许点名竞品**。
3. **peer 模式**:不许有 `is_focal=true`(demo 就是这档)。
4. **空题**:问题文本为空。

**🟡 告警(warn,不拦,给意见)**:
5. **配比**:不是 8/6/6/4 —— **phase=draft 时升级为硬错(AI 起草必须严格)**;phase=final 仅提醒(用户已改、有最终权)。
6. peer 模式下品牌防御题没点名任何已声明品牌 · 重复题 · 引导句式黑名单(`why is … best`、`the best … is`)命中。

> 闸只保证"没有明显违规";**质量与隐性引导仍由用户拍板**(尤其本地桶的场景真实性,机器查不出,必须人看)。
> 第 1 步只做 validate 函数 + CLI 子命令;**不接进 probe/run**(smoke/micro 配置照样能跑);焦点报告高亮属第 2 步。

---

## 7. 诚实约束(对齐 CLAUDE.md §7)

- **盲起草**:发现/比较/本地三桶,起草环节不知道焦点是谁。
- **AI 只起草,人拍板**:用户有完全修改权,AI 起草+建议绝不自动冻结。
- **一次性 + 冻结**:起草是 SETUP 一次性动作,人工确认后冻结成基线,**绝不进测量轨、绝不每次运行重生成**(否则基线断裂、不可复现)。
- **语言**:跟随冻结配置 locale。
