# 代码全审核指南(Code Audit & Modifiability Guide)

> **给谁看**:外部人类审核者 / 接手的 AI agent。
> **解决什么**:有反馈说"代码不好改、没有框架架构、改起来困难"。本文正面回应——
> ① 澄清"没用重型框架"与"没有架构"是两回事;② 给出**全代码地图**;
> ③ 给出**常见修改的操作手册**(改哪里、什么顺序、动哪些测试、护栏怎么接住你);
> ④ **诚实**列出真实的可修改性痛点 + 改进建议(§9,不回避)。
> **配套**:架构决策见 [ARCHITECTURE.md](../ARCHITECTURE.md);行为红线见 [CLAUDE.md](../CLAUDE.md)。
> 截至:180 测试绿,五检查员(ruff/mypy/import-linter/gitleaks/pytest)全过。

---

## 0. 先澄清:"没有框架" ≠ "没有架构"

这是个**有意的设计取舍**,不是疏漏:

- 本项目是 **可复现的代理测量框架**,护城河是**测量诚实 + 可审计**。重型 Web/ORM/插件框架会带来**魔法与间接层**(依赖注入容器、元类、运行时反射、约定大于配置),它们让"这个数字怎么来的"更难追溯——**与本项目的核心价值冲突**。CLAUDE.md §3 因此明令"反过度工程、不经确认禁止新增依赖"。
- 取而代之,本项目用一套**轻量但严格的分层架构**,并用 **import-linter 在 CI 里把层级边界焊死**(违规即红、不许合并)。运行时只有 **3 个第三方包**(pydantic / PyYAML / anthropic)。
- **所以可修改性的真相是**:这里没有"框架脚手架"帮你自动接线,但有**强类型契约 + 边界检查 + 黄金测试**组成的安全网——**改错会立刻、响亮地失败**,你可以放心重构。这对测量工具是比"框架魔法"更合适的可修改性(见 §6、§9)。

> 一句话:本项目优化的是 **"安全地改"**(改错被工具当场拦下),不是 **"快速堆功能"**(靠框架自动接线)。

---

## 1. 全代码地图(按层 + 行数 + 何时动它)

`src/aivis/` 共 ~3900 行,17 个测试文件 ~2660 行。**文件小、单一职责**是有意的。

### ① 契约层 `contracts/`(🟥 载重墙;改这里 = 影响所有下游,先问人)
| 文件 | 行 | 职责 | 你想改 X 时来这 |
|---|---|---|---|
| `records.py` | 270 | 所有跨模块数据形状(pydantic,运行时校验):`RawResponse/Citation/RunManifest/EvidenceRecord/BrandMention/CitationAttribution/ExtractionResult/Metrics/TierAssignment/ScoreBundle/HealthSummary/Interval` | 加字段/加指标的数据形状 |
| `config.py` | 85 | `FrozenConfig/Brand/Question/VerifyThresholds` + `fingerprint()` | 加配置项、改阈值 |
| `adapter.py` | 51 | `ProviderAdapter` Protocol(引擎扩展点) | 加引擎接口 |
| `checker.py` | 46 | `UrlChecker` Protocol + `CHECK_*` 常量(核验扩展点) | 加核验方式接口 |

### ② 纯内核(🟥 禁网络/SDK/存储;import-linter 强制)
| 文件 | 行 | 职责 |
|---|---|---|
| `pipeline/extract.py` | 233 | 从回答抽提及 + 引用归属;保守计数(清晰名计分/歧义记录不计分);owned vs earned |
| `pipeline/score.py` | 308 | 三档判定 + 六指标 + Wilson 区间 + 健康灯(`score()` 主函数 + `_health/_stability`) |
| `stats/wilson.py` | 30 | Wilson 区间纯计算 |
| `templater.py` | 78 | 免费档模板起草 24 题(纯;品类/品牌填空) |

### ③ I/O 边缘
| 文件 | 行 | 职责 |
|---|---|---|
| `pipeline/probe.py` | 100 | **唯一写原始层**;成本闸门(估算→人工确认→运行时硬封顶)+ 幂等续跑 |
| `pipeline/verify.py` | 346 | **唯一抓被引页面**;SSRF 防护 + 重定向逐跳校验 + HTML 消毒;`OfflineChecker/HttpHeadChecker/PageFetchChecker` |
| `pipeline/report.py` | ~250 | 公共入口:渲染 Markdown / CSV + 再导出 `render_html`(import 路径稳定) |
| `pipeline/report_html.py` | ~330 | HTML 报告组装(render_html + 单/多品牌排版 + 矩阵/证据 helper) |
| `pipeline/report_assets.py` | ~135 | 报告叶子件:常量(淡橙/tier)+ 数字格式化 + 内联 SVG(无 I/O) |
| `matching.py` | ~35 | 🟥 单一品牌匹配口径(name_pattern/find_positions/mentions_surfaces),纯 |
| `providers/mock.py` | 177 | mock 引擎(零成本默认;fixture + 播种模板) |
| `providers/anthropic.py` | 289 | 真 Claude 引擎(web_search)+ 成本估算/计价 |
| `providers/drafter.py` | 298 | AI 档起草(盲中立 + 防御 + 竞品;`TextSender` 抽象网络) |
| `storage/raw.py` | 87 | 原始层(🟥 只 append/read,无 update/delete) |
| `storage/evidence.py` | 40 | 证据层(按 URL 去重缓存) |
| `storage/analysis.py` | 48 | 分析层(派生产物,可重写) |
| `configio.py` | 29 | YAML → `FrozenConfig` 加载 |

### ④ 编排壳(🟥 薄如纸)
| 文件 | 行 | 职责 |
|---|---|---|
| `cli.py` | 286 | 子命令转发到 pipeline(probe/extract/verify/score/report/run/validate/init/estimate) |
| `configcheck.py` | 193 | 配置质检(反偏置/配比/焦点不变量;纯函数) |
| `scaffold.py` | 139 | `aivis init` 生成焦点配置骨架 |

---

## 2. 数据流与三个扩展点

```
            ┌─ extract(纯)─┐
probe → ①raw               ├─→ score(纯)→ report(渲染)
            └─ verify(IO)──┘
② evidence ────────┘        ③ analysis
```

**只有三个被设计成"加实现、不改核心"的扩展点**(其余一律走契约):

| 扩展点 | Protocol | 加新东西怎么做 | 谁不用动 |
|---|---|---|---|
| 引擎 | `contracts/adapter.py: ProviderAdapter` | 在 `providers/` 加一个类 | pipeline 全部(🟥 禁 `if provider==`) |
| 核验方式 | `contracts/checker.py: UrlChecker` | 在 `verify.py` 加一个 `*Checker` 类 | score/extract/storage 全部 |
| 数据形状 | `contracts/` | 改一处契约 | mypy/pydantic 把错误推到所有调用点 |

---

## 3. 修改操作手册(直接回应"难改")

每个手册:**动哪些文件 → 什么顺序 → 更新哪些测试 → 哪个护栏会接住你改错**。

### 3.1 加一个新指标(如 "answer length")
1. `contracts/records.py`:给 `Metrics` 加字段 → **mypy 立刻报所有缺字段处**(编译器替你列出待改清单)。
2. `pipeline/score.py`:在 `score()` 的品牌循环里算它、塞进 `Metrics(...)`。
3. `pipeline/report.py`:想展示就加一列/一块。
4. 测试:`tools/regen_golden.py` 重生成黄金 + `python tools/regen_examples.py` 重生成 HTML 样张;人工核对 diff;说明口径。
- 护栏:黄金测试红(口径变)+ mypy 红(字段缺)= 改漏了当场知道。

### 3.2 加一个引擎(已接五家:anthropic / openai / gemini / perplexity / grok;grok 复用 openai SDK,第 4 步无新依赖)
1. `providers/catalog.py`:加 `ProviderInfo`(密钥环境变量名 + 唯一发往主机)和各模型的 `ModelInfo`
   (含诚实标记:supports_temperature / supports_region / pinned / surface_kind)+ 档位默认表。
2. `providers/<engine>.py`:写一个类实现 `ProviderAdapter`(provider_name/model_name/info/ask/
   estimate_cost_usd/actual_cost_usd…),单价表放本模块,唯一网络触点 `_send()`。
3. `providers/registry.py: make_adapter`:加一个分支(这是**唯一**该出现引擎名的地方;SDK 在分支里惰性 import)。
4. `pyproject.toml`:依赖白名单加 SDK(经人确认),import-linter 纯层禁入名单加它;`.pre-commit-config.yaml`
   两个本地钩子的 additional_dependencies 同步加。
5. 测试:`tests/test_<engine>_adapter.py` 用替身覆盖网络触点(仿 `test_openai_adapter.py`);
   `tests/test_registry.py` 的目录计数与默认表一致性测试会要求你把目录改齐。
- 护栏:import-linter 保证你没把 SDK 漏进纯层;`ProviderAdapter` 协议保证形状齐全。

### 3.3 加一个核验方式
1. `verify.py`:加一个 `*Checker` 类实现 `UrlChecker`(`method` 属性 + `check(url)`)。复用共享 `is_safe_url`(SSRF)。
2. `cli.py: _cmd_verify`:`--check` 多一个选项。
3. score **不用改**(三档判定按 `check_method` 自动取口径;新方式记得在 `score._TIER_BASIS` 命名)。
- 护栏:证据缓存按 `check_method` 区分,换口径必触发重核验、不会静默混用(ARCHITECTURE §4.5)。

### 3.4 改报告样式
- Markdown/CSV:`report.py` 的 `render_markdown`/`render_csv`;HTML 结构:`report_html.py`
  (`render_html` + `_brand_overview_html/_single_brand_overview_html/_matrix_html/_evidence_html`);
  颜色/CSS/SVG/数字格式化:`report_assets.py`(`_HTML_CSS/_ORANGE/_bar_svg/_donut_svg/_pi/…`)。
- 改完跑 `python tools/regen_examples.py` + 人工核对 `examples/single_brand_report.html` diff;`test_examples` 守"文件 == 渲染输出"。
- 🟥 外部/AI 数据必须 `html.escape`(防注入);不许引外链/脚本(`test_e2e_html_report_is_offline_single_file` 守)。

### 3.5 加一个配置字段
1. `contracts/config.py`:给 `FrozenConfig`/`Brand`/`Question` 加字段(带默认值则旧配置兼容)。
2. `configio.py`:若 YAML 结构变需对应解析。
3. 注意:**改配置 = config_fingerprint 变 = 新基线**(机械暴露,见 ARCHITECTURE §5)。

### 3.6 改起草 prompt / 免费模板
- AI 档 prompt:`providers/drafter.py` 的 `_NEUTRAL_PROMPT/_DEFENSE_PROMPT/_COMPETITOR_PROMPT`(改前读 [drafting-review-principles.md](drafting-review-principles.md),这是诚实最敏感处)。
- 免费模板:`templater.py` 的 `_DISCOVERY/_COMPARISON/_LOCAL/_DEFENSE`。
- 护栏:`configcheck.validate_config` 反偏置(中立题含品牌名=硬错)+ test_templater/test_drafter。

### 3.7 加一个 CLI 子命令
- `cli.py: _build_parser` 的 `command` choices + `main()` 的分发 + 一个 `_cmd_*` 函数(逻辑放可 import 的库,不写进 cli)。
- 痛点见 §9.2(if/elif 分发)。

---

## 4. 不变量与护栏(审核者可信任这些)

红线不是口号,是 CI 里的检查员(pre-commit 本地 + CI 权威,红了不许合并):

| 护栏 | 工具 | 拦什么 |
|---|---|---|
| 层级边界 | import-linter | 纯层 import 网络/SDK;contracts 依赖上层;跨层乱引用 |
| 类型一致 | mypy --strict(查 src+tests)+ pydantic 运行时 | 形状不对、字段缺失 |
| 密钥不入库 | gitleaks | 疑似密钥提交 |
| 风格/死代码 | ruff (+format) | lint + 统一格式 |
| 口径不漂移 | pytest + 黄金样本 | ScoreBundle 黄金 / HTML examples 任何无意改动 |
| 原始层只追加 | 设计 + 单测 | raw store 无 update/delete 方法 |

**对审核者的含义**:你不必逐行读核心逻辑就能信任边界——**违反即红**。想验证?故意把红线改回去(如让 extract import requests),`import-linter` 当场失败。

---

## 5. 测试策略

- **mock-by-default**:测试/CI/示例全走 mock,零成本不联网。真实调用是显式、可关、默认关。
- **纯内核配黄金/单测**:extract 16 / score 10 / wilson 6 / templater / drafter…
- **黄金纪律**(CLAUDE.md §10):`golden_e2e_bundle.json`(冻结整个 ScoreBundle)+ `examples/single_brand_report.html`(冻结 HTML)。有意改口径 → 跑对应 `tools/regen_*.py` + 人工核对 diff + PR 说明。
- **数据驱动 + 导入有测试钉死**:`test_html_auto_updates_with_data`(换数据→HTML 变)、`test_bundle_store_roundtrip_then_render`(写入→读回无损→渲染一致)。

跑法:`pytest -q` / `ruff check .` / `mypy` / import-linter。

---

## 6. 审核者导航路线(按目的)

| 想审 | 去哪 |
|---|---|
| 正确性(指标口径) | `pipeline/score.py` + ARCHITECTURE §6;黄金 `tests/test_score.py`/`test_e2e_mock.py` |
| 安全(SSRF/注入) | `pipeline/verify.py`(`is_safe_url`/重定向/`html_to_text`);`tests/test_url_safety.py` |
| 测量诚实(防偏) | `configcheck.py` + `templater.py`/`providers/drafter.py` + [drafting-review-principles.md](drafting-review-principles.md) |
| 成本 | `providers/anthropic.py`(estimate/actual)+ `pipeline/probe.py`(闸门) |
| 可修改性 | **本文 §1/§3/§9** |
| 该往哪开火(深挑战) | 成本泛化 / 盲起草强度 / SSRF 绕过 / 红线死角 / 采样不足 |

---

## 7. 诚实的可修改性自评(不回避审核者的批评)

审核者说"难改"——**部分成立**。逐条摊开,哪些是真痛点、哪些是有意取舍:

### 已修复(第二轮审核后,黄金字节不变 = 零行为改变)
1. ✅ **`report.py`(原 621 行)已拆**为 `report_assets.py`(常量/格式化/SVG)+ `report_html.py`(HTML 组装)+ `report.py`(markdown/csv + 再导出 render_html,import 路径不变)。
2. ✅ **`score.score()` 大函数已拆**为 `_aggregate_mentions/_aggregate_citations/_build_tiers/_build_metrics` 纯函数;`score` 只编排。
3. ✅ **品牌匹配口径统一**为 `matching.py`(extract/verify/configcheck 共用),不再"质检放过但计分命中"。

### 仍存在的真痛点(建议改进)
1. **`cli.py` 用 if/elif 分发子命令**(286 行)。加命令要动 parser choices + main 分发两处。
   - 建议:子命令注册表(`dict[str, Callable]`)或 argparse subparsers,把每个 `_cmd_*` 自注册。
2. **新增"一个全新流水线阶段"无框架支持**(只有 provider/checker 两个预留扩展点)。
   - 但 pipeline 是 5 个显式函数,插一段是直白的事(不是反射/钩子,是看得见的调用)。
3. **`actual_cost_usd` 解析各适配器自己的 `raw_json` 格式**,跨适配器误用会算错(handoff §10.7 已列)。

### 有意取舍(不建议盲目"框架化")
- **不做依赖注入容器 / 插件发现 / ORM**:会引入魔法与依赖,伤可审计性(§0)。
- **存储用 JSON 文件不用数据库**:MVP 数据量小,少一个活动部件(ARCHITECTURE §8)。
- **CLI/SKILL 薄如纸**:逻辑必须在可被 import、可测试的库里——这是特性。

> 结论给审核者:可修改性的**安全性**很高(强类型 + 边界 + 黄金,改错当场红)。第二轮审核点出的 report.py/score/匹配口径三处毛刺已拆分修复(黄金字节不变);剩 cli.py 分发是已知小毛刺。这不是"没有架构",是"架构刻意最小化 + 用工具而非框架来保证"。

---

## 8. 给接手 AI 的硬约束(摘自 review handoff)

- 不声称测"AI 排名真相";核心指标不用 AI 判读;strict/expanded 不混算。
- 不在测试/CI 跑付费 provider;不把密钥写进项目文件。
- 改契约前先更新受影响的测试与报告语义;保持 import 边界。
- 每次改完跑 `pytest -q` / `ruff` / `mypy` / import-linter。
