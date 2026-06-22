# 成本透明 + run 档位三档（计划）

> 目标：让用户在**每一个花钱的决策点**都清楚三件事——① 这一步/这一档**是什么、为什么**
> ② 跟别的选项**差别在哪** ③ 这一步/这一档**预估花多少**。成本必须透明、可预估、有语境。
> 本文件是该功能的格式与分步基础；逐步实现，每步封装、测试全绿、文档同步。

## 0. 红线对齐

- 🟥 **单价不编**：Haiku 真实单价在实现 Step A 前查证后再写（CLAUDE §6.3：成本按
  provider×model×tool_path 取价，禁止写死臆造单价）。
- 🟥 **mock 恒 0**：所有档位在 mock 下估价为 0，不破黄金（CLAUDE §6.1）。
- 🟥 **估算是保守上界**：run 估价沿用现有口径（n_calls × 单价上界，serial 模式 = n_runs 上限）。
- 文案是用户最终看到的东西，英文（与 GUI 一致）；本计划用中文记录。

## 1. 单一真相源：档位元数据

所有"档位说明 + 模型 + 定价"集中一处定义，GUI / CLI / 估算共用，禁止各处散落文案。

### 1.1 run 档位（providers 层，anthropic 特有模型知识）

```
TierInfo(frozen):
  id:    "economy" | "standard" | "high"
  model: "claude-haiku-4-5" | "claude-sonnet-4-6" | "claude-opus-4-8"
  label: "Economy" | "Standard" | "High quality"
  blurb: 一句话意义/定位（英文）
```

| id | 模型 | 定位文案（草稿，英文） | 单价 $/Mtok (in/out) |
|---|---|---|---|
| economy | claude-haiku-4-5 | "Cheapest & fastest, lower depth — a quick first look or tight budgets." | **待查证** |
| standard | claude-sonnet-4-6 | "Balanced default — strong quality at moderate cost. Recommended for most baselines." | 3 / 15 |
| high | claude-opus-4-8 | "Most capable — best reasoning & accuracy, highest cost. For a final authoritative baseline." | 5 / 25 |

差距一句话（展示在三档下方）：质量/深度依次升高，成本随之升高；mock 恒 $0 只验链路。

### 1.2 起草档（已存在，补元数据 + 估价）

| id | 意义（英文草稿） | 与另一档区别 | 成本 |
|---|---|---|---|
| free | "Fixed 24-question template; your category & brand are locked chips; $0, no key." | 通用模板，本地/场景题偏泛 | $0 |
| ai | "Claude drafts all 24 questions tailored to your industry (neutral ones drafted blind, without your brand) and suggests competitors." | 更贴行业、尤其本地/场景题；自动建议竞品 | ≈ 单独估价（下） |

## 2. 花销总览（每步都要有）

- **Step2 起草**：免费档显示 `$0`；AI 档显示**单独预估**（≈ 起草调用数 × 单价上界）。
- **Step3 run**：选中档显示 `≈ $X（本配置）` + 是否超 budget_cap。
- **合并总览**：展示"起草估 + run 估 = 本次大概总额"，让用户一眼看到全程花销。

## 3. 分步实施（一步一封装）

- **Step A — 引擎**：① 查证 Haiku 真实单价 → 定价表 + `_TIER_MODELS` 加 `economy`；
  ② 加 `TierInfo` 元数据（含三档 blurb）为单一真相源；③ 单测 + 不破黄金（mock=0）。
  CLI `--tier` 顺带支持 `economy`。
- **Step B — GUI 后端**：① 分档估价：一个端点对当前配置返回三档 `[{id,label,model,blurb,
  est_usd,over_budget}]`；② AI 起草单独估价（起草调用数的成本上界）。
- **Step C — GUI 前端**：① Step3 三档卡片（label+model+blurb+本配置估价+超预算标记，选中即用）；
  ② 三档下方"差距"说明；③ Step2 AI 档:意义 + 与免费档区别 + 单独预估金额;④ 每步花销总览
  + 合并总额。
- **Step D — 收尾**：README/SKILL 同步三档 + 成本透明；全测试绿、五检查员过；记忆封装。

## 4. 影响面/注意

- 加 `economy` 进 `_TIER_MODELS` / 定价表 = 纯增量，不影响现有 standard/high 与黄金。
- GUI 后端 `api.estimate` 已支持按 `tier` 估价；分档估价 = 对三档各调一次现成估算。
- configcheck/引擎不设硬性档位限制；GUI 仅做选择 + 展示。
- 验收：`mypy` / `pytest -q` / `ruff check .` / import-linter 全绿；改报告/口径才动黄金。
