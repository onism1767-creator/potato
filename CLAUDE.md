# CLAUDE.md — 给 Claude Code 的厂规（本仓库的最高行为约束）

> 这份文件是 Claude Code 在本仓库工作时**每次会话都必须先读**的规则书。它的目的只有一个：让 AI agent 在高速写代码时**撞不穿架构边界**，从而不把这个项目写成屎山。
> 规则分两类：**🟥 红线**（任何情况下不许越；越线即视为错误，必须停下来问人）和 **🟧 约定**（默认遵守，确有理由偏离要先说明）。
> 本文件的"项目定位 / 架构边界 / 数据契约 / 安全 / 可复现"几节是**锁定的**，与具体功能清单无关；只有 §11 的"第一版范围"会随产品决策更新。

---

## 0. 一句话项目定位（先理解再动手）

本项目是一个**开源的 Claude Skill（SKILL.md）+ 命令行工具（CLI）+ 本地图形向导**，用来测量一个品牌/网站在 **AI web-search 回答**（v0.2 起：Claude / OpenAI / Gemini / Perplexity 四家，2026-11 加 Grok，每家若干模型，每个 provider×model 是一个独立**测量表面**）中被**提及（mention）**和**被引用（citation）**的频率与结构。

它的本质是一个 **可复现的代理测量框架（reproducible proxy measurement framework）**：
- **不是**爬虫；是反复用一批冻结的问题去"问"AI，收集回答 + 引用 URL，再用**确定性规则**打分。
- **不是**"AI 排名真相检测器"；只测"某个引擎的 web-search 表面在某配置下的表现"；多表面**只并列、不混总分、不跨引擎排名**。
- 输出可追溯、可审计、带不确定区间的报告。

### 🟥 叙事红线（写注释、文案、报告、README 时都适用）
1. 不许把本工具说成"测出了 AI 曝光度真相 / AI 排名"。统一措辞：**"AI web-search 可见度（代理测量）"**，并总是点明是哪个引擎、哪个模型。
2. 报告/输出里凡涉及结论，必须可标注 `surface_id` 与"**本结果仅代表此引擎**"。
3. 行动层只给**线索（leads）**，**绝不**生成"照做就能让 AI 提到你"的保证或建议清单。本工具是观测工具，不是 recommendation engine。

---

## 1. 架构层级与不可跨越的边界（🟥 核心红线）

数据严格分三类、单向流动，**禁止回流、禁止跨层直读对方内部状态**：

```
① 原始观测 (raw)        ② 核验证据 (evidence)      ③ 分析与行动 (analysis)
provider 原样返回    →   被引 URL 的核验/分类    →   指标/区间/gap/driver/报告
    [只追加]                [可重算]                   [完全派生]
```

核心流水线，每段职责单一、边界清晰：

```
probe  →  extract  →  verify  →  score  →  report
(I/O)     (纯)        (I/O)      (纯)       (渲染)
```

### 🟥 边界红线
1. **只有 `probe` 写原始层（raw store）。** 原始层 **append-only**，任何其它模块不许覆盖、不许改写已写入的原始记录。
2. **`extract` 和 `score` 必须是纯函数**：输入数据、输出数据；**禁止**网络请求、文件读写、读系统时钟、未播种的随机数。它们不许 `import` 任何网络/HTTP/SDK 库（见 §3）。
3. **只有 `verify` 做被引页的抓取**，结果写证据层；`score` 只读证据层、不自己抓页面。
4. **跨层只走契约对象**（§2），不许一个模块 reach into 另一个模块的内部字典/私有属性。
5. 一个文件 / 一个模块**只做一件事**。新逻辑不确定该放哪一层 → **停下来问人**，不要"就近塞"。

---

## 2. 数据契约规则（🟥 单一真相源）

所有跨模块传递的数据形状，**只在 `src/aivis/contracts/` 里定义一次**，用 pydantic（或 dataclass）带类型。包括但不限于：
`RunManifest` / `RawResponse` / `Citation` / `BrandMention` / `EvidenceRecord` / `Metrics` / `FrozenConfig`。

### 🟥 契约红线
1. **禁止在 `contracts/` 之外就近发明新的数据结构**（不许在 pipeline 里临时造一个"差不多但不一样"的 dict 来传数据）。
2. 需要新字段/新类型 → **先改 `contracts/`，再改用它的地方**；改契约属于"会影响所有下游"的动作，**必须先问人**。
3. `RunManifest` 必含字段（缺一不可，用于可追溯）：
   `provider / surface_id / model / tool_path / tool_version / locale / region / prompt_version / competitor_set_version / max_uses / budget_cap_usd / run_id / timestamp / search_count`。
4. 冻结配置（`FrozenConfig`）来自 `questions.yml` + `brands.yml`，是**显式传入的不可变对象**，不许做成全局可变状态或环境隐式读取。**任何配置改动 = 版本号 bump（新基线）**，不许静默混入既有趋势序列。

---

## 3. 依赖红线（🟥 防过度工程）

agent 最常见的破坏之一是"顺手"引入重型抽象。本仓库：

1. **不经人确认，禁止新增任何第三方依赖。**
2. **MVP 存储 = SQLite / Postgres-JSONB + 普通文件（JSONL）。禁止引入向量数据库 / 向量检索**（计划书明确：首阶段不是 RAG 应用；语义检索留到很后期且需单独决策）。
3. `extract` / `score` / `stats` 所在的纯层，**禁止** import `requests` / `httpx` / `anthropic` / `openai` / `google`（genai）/ `perplexity` / 任何网络或 SDK 库。
4. 不要自创框架式抽象层、插件系统、ORM 魔法。**Provider 适配器是唯一允许的"接口预留"抽象**（见 §4）。

---

## 4. Provider 适配器规则（🟥 多引擎在架构层解决，不在调用处 if-else）

1. 所有引擎都藏在 `contracts/adapter.py` 的 `ProviderAdapter` Protocol 后面：输入（问题/区域/语言/温度/`max_uses` 等）→ 标准化输出 `RawResponse`（回答正文、引用列表、搜索次数、模型名+版本、`tool_path`、错误码）。
2. **v0.1 只有 `mock` 与 `anthropic`；v0.2（2026-09）起接入 `openai` / `gemini` / `perplexity` 四家真引擎。** 模型目录（纯数据、不 import SDK）在 `providers/catalog.py`，唯一工厂在 `providers/registry.py`——**这是全仓库唯一允许按引擎名分支的地方**。接一个新引擎 = 新适配器模块 + 目录条目 + 离线替身测试，pipeline 零改动。
3. **禁止在 pipeline / GUI / CLI 里用 `if provider == "anthropic"` 这类分支** 处理引擎差异；差异必须封装在各自适配器内，选择一律经 registry。
4. Anthropic 适配器要点（实现时对照官方文档复核）：调用 web_search 工具（经典版 `web_search_20250305`，见 ARCHITECTURE §9）；引用始终开启；工具报错以 **HTTP 200 + error block** 返回，需**捕获并记为失败**（不是空回答）；`refusal` 停止原因记为失败且**不做 fallback**；每次记录精确模型名 + 日期。
5. **每个表面的诚实标记必须在目录里声明并进清单**：`supports_temperature`（拒绝采样参数的模型不传温度）/ `supports_region`（无区域参数的 API）/ `pinned`（底层模型会自动更新的预设档）/ `surface_kind`（`search_only` = 无回答文本，提及类指标不适用）。probe 把它们写进 `RunManifest`（`temperature_applied` / `region_applied` / `surface_kind`），报告如实呈现；不许替引擎"补齐"它做不到的事。
6. 引擎给的引用 URL 不是真实网址时（Gemini 的 grounding 跳转链接），只许在适配器里**读跳转头解析**（不抓正文、不出该跳转域），原始链接原样留在 raw_json；解析失败保留原链接交 verify 如实核为无法归属。

---

## 5. 安全红线（🟥 不可协商）

1. **API 密钥只经环境变量 / GitHub Secrets 读取**；日志必须脱敏（redaction），**禁止**把密钥写进代码、配置文件、日志、报告。
2. **不处理 PII**；默认合规模式从严。
3. **所有抓取的网页与所有 AI 回答一律当作不可信数据（prompt-injection-safe）**：从规则层抽取，**绝不**把生 HTML / 网页文本直接当指令喂给模型；做 HTML 消毒；用域名 allow/blocklist。
4. 最小权限 `GITHUB_TOKEN`；fork PR 不暴露 secrets。
5. 每次运行前强制 **budget / call cap**；探测是 bounded run，不许无上限深搜。

---

## 6. 成本红线（🟥）

1. **mock-by-default**：测试、CI、示例默认走 mock，**不花钱**。任何会真实调用付费 API 的代码路径，必须显式、可关、且默认关。
2. 真实运行前必须经过**成本闸门**：估算（≈N 次调用，≈$X）并在 caller 层留出人工确认点；超 `max_uses` / 最大轮次 / 月度预算阈值则阻断或降级。
3. 成本计算按 **provider × model × tool_path** 取价，**禁止写死静态单价**。
4. 先核 headline / gap 结论，长尾按需——不要默认对所有东西做最贵的深度核验。

---

## 7. 可复现与测量诚实（🟥 这是项目的护城河，不许偷工）

1. **核心指标不许用 AI reader**：提及/引用的计数、owned/earned 判定、份额、位置、稳定性，全部由**确定性规则**算出。AI 只允许出现在：①起草问题/建议竞品（SETUP，人工确认后冻结）②叙述报告（仅描述，不进指标）③默认关闭的情感层。
2. **保守计数（CONSERVATIVE COUNTING）**：只有清晰品牌名才计分；缩写/歧义进 `extra data`，**记录但不计分**。
3. **三档口径**：`mentioned → cited → verified`，**只有 verified 档进入核心评分**。
4. **clean-room 探测**：每次调用都是全新、无历史、无个性化上下文；pinned 区域/语言/温度。
5. 比例类指标默认带 **Wilson 区间**；**每个数字都以区间/范围呈现**，差距未超过最小可区分阈值就归入"无法区分"，不许给假精确的排名。
6. 每次运行写完整 `RunManifest`；模型漂移 = 基线断裂，必须可追溯。

---

## 8. 目录结构（🟧 约定，新文件按此归位）

```
ai-visibility/
  CLAUDE.md            # 本文件
  ARCHITECTURE.md      # 为什么这么分层（决策记录）
  SKILL.md             # Claude Skill 入口（薄；SETUP / MONITORING 两模式 + 成本闸门）
  pyproject.toml       # 依赖 + ruff / mypy / import-linter 配置都放这
  .gitignore           # 忽略 .env、密钥、缓存
  .pre-commit-config.yaml   # 本地提交时的检查员（§10.5）
  .github/workflows/ci.yml  # 推送/PR 时的检查员（§10.5）
  src/aivis/
    contracts/         # 🟥 载重墙：records.py / adapter.py / config.py（先写、冻结）
    providers/         # catalog.py（模型目录，纯数据）/ registry.py（唯一工厂）/ mock.py /
                       # anthropic.py / openai.py / gemini.py / perplexity.py / grok.py（每家一个适配器）
    pipeline/          # probe.py / extract.py(纯) / verify.py / score.py(纯) / report.py /
                       # report_compare.py（多表面并列对比，纯渲染）
    stats/             # wilson.py / bootstrap.py / sequential.py（全纯）
    storage/           # raw(只追加) / evidence / analysis 三个边界
    cli.py             # 🟧 薄：子命令转发到 pipeline，不写业务逻辑
  tests/
    fixtures/          # 黄金原始 JSONL / 黄金页面 / 黄金期望指标
  configs/demo/        # 5 品牌中立 demo：questions.yml / brands.yml
```

CLI 子命令：`probe` / `extract` / `verify` / `score` / `report` / `run` / `models` / `estimate`；全局 flag `--provider`（默认 mock）、`--model`（精确 id）、`--tier`（档位便利选项）、`--surfaces`（多表面 provider:model 列表，逐个跑 + 并列对比 + 账单）。
**🟥 CLI 与 SKILL.md 都薄如纸**：只负责编排，所有逻辑在可被 import 的库里（否则逻辑漏进测不到的地方）。

---

## 9. 代码风格与命名（🟧 约定）

1. 全程类型注解；纯层尽量纯函数，避免隐藏可变状态与全局变量。
2. 命名用领域词汇并与契约一致：`focal`（焦点品牌=用户自己域名）/ `peer`（同行集）/ `competitor` / `owned` vs `earned` / `mention` vs `citation` / `stratum`（采样分层）。
3. 小文件、单一职责；函数短、副作用集中在 I/O 边缘。
4. 错误要显式（记为失败并保留上下文），**禁止**静默吞异常导致"部分失败被当成空结果"。

---

## 10. 测试与"完成的定义"（🟥 done 的硬标准）

一个任务"做完"当且仅当：
1. **相关测试全绿**，CI 绿。
2. **黄金样本（fixtures）未变**——除非本次任务的目的就是有意改它，并在说明里写清为什么。
3. 新增的纯逻辑（extract/score/stats）**必须配黄金或单元测试**；改了指标口径必须更新对应黄金期望并说明。
4. 不许为了让测试过而删测试 / 放松断言。

---

## 10.5 自动检查员（CI 守卫）—— 让红线从"规则"变成"保证"（🟥）

光把红线写在本文件里还不够：下列"自动检查员"**必须存在**，并在**每次提交（pre-commit）和每次推送（CI）**时自动运行，**不过就拦下、不许合并**。Claude Code 在搭项目骨架时要把它们一并建好。所有检查默认 **mock / 离线运行**，不花钱、不联网。

1. **层级检查员（守"纯层不接网络""不跨层乱引用"）**：用 `import-linter` 定义层级契约——`contracts` 不依赖任何上层；`extract` / `score` / `stats` 禁止 import 网络或 SDK 包（`requests` / `httpx` / `anthropic` 等）；`cli` 不直接写业务逻辑。违规即 CI 失败。

2. **密钥检查员（守"密钥不入库"）**：用 `gitleaks`（或 `detect-secrets`）做 pre-commit + CI 扫描；仓库提供 `.gitignore` 忽略 `.env`、密钥文件。一旦疑似密钥被提交，直接拦下。

3. **类型检查员（守"大家用同一套标准零件"）**：用 `mypy`（或 `pyright`）做静态类型检查，CI 必须零错误；契约对象用 pydantic，运行时也校验形状不对的数据。

4. **格式/质量检查员**：用 `ruff` 做 lint + 格式化（pre-commit 自动修、CI 复查）；统一风格，挡掉死代码和明显坏味道。

5. **测试报警器（守"完成的定义"）**：用 `pytest` 在 CI 跑全部测试 + 黄金样本；任何测试变红、或黄金期望被无意改动，CI 失败（呼应 §10）。

6. **原始层只追加（守"原始数据不许改"）**：这条用**设计 + 测试**保证，不靠 lint——`storage/raw` 只暴露 `append` / `read`，**不提供** `update` / `overwrite` / `delete` 方法；并配一个单元测试断言"raw store 没有任何会改写已有记录的方法"。

把以上串起来的两层关卡：**pre-commit**（你本地每次提交时自动跑，快）+ **GitHub Actions CI**（每次推送 / PR 自动跑，权威，红了不许合并）。

> 一句话：本文件第 1–7 节是"墙上的规章"，本节是"门口的检查员"。两者都在，红线才真的拦得住。

---

## 11. 版本范围（🟧 会随产品决策更新；当前锁定如下）

**v0.2 已做（2026-09，多引擎）**：四家真引擎适配器（Claude 6 / OpenAI 6 / Gemini 5 / Perplexity 4，含 Search API 纯搜索表面）· 模型目录 + 注册表 · `--model` / `--surfaces` 多表面串跑 · 并列对比报告（只并列不混总分）· 账单（清单记真实花费）· 清单诚实字段（温度/区域是否生效、表面类型）· GUI 多选引擎 + 每家密钥框 + 合计预估 · `models` 子命令。**未做**：跨引擎并行跑（串行）· 整批总预算上限（仍是每表面各自受限）· Gemini Interactions API（仍用 generateContent）。

**v0.2 之后的增量（2026-11）**：第五家 Grok（xAI，6 个表面，共 30 个表面）——复用 `openai` SDK 指向 `api.x.ai`，未加依赖；只开 `web_search`（不开 `x_search`）；无区域参数；零宽"浏览来源"标注不算引用。详见 ARCHITECTURE §11。

**v0.1 要做（IN，已完成）**：`contracts/` 全部冻结类型；`mock` 适配器；三类数据存储（raw 只追加，JSONL+SQLite 即可）；`probe→extract→verify→score→report` 在 mock 上端到端跑通，产出最简真实的 **Markdown + CSV** 报告；`extract`（owned/earned + 保守计数）与 `score`（六指标 + Wilson 区间）真实实现 + 黄金测试；`verify` 最小版（URL 规范化 + 可达性 + 来源分类 + 三档标签，**不含真页面抓取摘录**）；薄 CLI；`configs/demo/` 5 品牌中立 demo；CI 绿。

**仍未做（OUT）**：driver tags / 行动层 · 情感层 · 运营轨 · 任何 SaaS 件（workspace/RBAC/告警/审计）。（v0.1 的 OUT 项里，真 Anthropic 适配器、序贯采样、verify 真页面抓取、HTML 报告、bootstrap 区间均已在后续版本落地。）

> 这些 OUT 项不是"永不做"，是"功能慢慢加"——但**只能在地基（契约 + 纯内核 + 测试网）绿了之后，一次加一块，每块躲在它稳定的契约后面加**。

---

## 12. 接受任务时的工作流（🟥 agent 自我约束）

1. **规格 + 测试先行**：拿到任务先确认它对应哪一层、用哪些契约、要让哪些 fixtures 过；先写/读测试，再实现。
2. **小步、单一关注点**：一次只改一层/一个模块，让 diff 小到能整体审完。
3. **不许跨边界顺手重构**：要改契约、要加依赖、要动另一层、要新建抽象 → **先停下来问人**，不要自作主张。
4. **跨会话先读** `CLAUDE.md` + `ARCHITECTURE.md` 再动手，避免重新发明已定的结构。
5. 拿不准就**保守**：宁可少做、问清楚，也不要"猜一个看起来合理的设计"铺开写。

---

## 术语表（避免歧义）

- **focal brand**：用户自己的域名；**peer set**：中立 demo 里的同行集（无指定焦点）；**competitor**：用户自己确认的竞品。
- **owned vs earned**：被引来源是品牌自有域名（owned）还是第三方页面（earned），两者分开计，**不混**。
- **mention → cited → verified**：三档证据强度，只有 verified 进核心分。
- **SOV**：share of voice（份额），无竞品时整体移除。
- **AVS**：复合分，**去强调**——只作带标签的头条 + 公式，不作驱动 KPI。
- **stratum**：采样分层维度（区域×语言×模型档位），同层内才聚合比较，跨层只并列不混总分。
