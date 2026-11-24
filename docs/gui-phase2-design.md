# GUI Phase 2 设计 —— 本地向导 `aivis gui`(2-B 完全自助)

> 配套 [distribution-options-review.md](distribution-options-review.md)(四阶段路线 · Phase 2 是本文)、
> [setup-flow-spec.md](setup-flow-spec.md)(SKILL 对话式 7 阶段,本 GUI 是它的可视化版)、
> [question-drafting-principles.md](question-drafting-principles.md)(起草规则书,起草器照此实现)。
> 状态:**已实现**(2026-06 落地,之后多次迭代)。本文是当时的设计记录,细节(只连 Anthropic、暖橙配色、三档等)已被后续版本取代,现状以 README 与代码为准。

---

## 0. 定位与边界(🟥)

`aivis gui` = 一个**只监听 `127.0.0.1` 的本地网页向导**,后台**调现成 CLI/库**,不重写引擎。

- **薄壳**:GUI 只做编排——收集输入、调起草器/质检/冻结/estimate、起 `aivis run` 子进程、开报告。所有测量逻辑仍在引擎里(CLAUDE.md §8 cli/SKILL 薄如纸,GUI 同此约束)。
- **零新第三方依赖**(CLAUDE.md §3):网页用标准库 `http.server`;异步用 `subprocess`;key 只在内存;AI 起草复用现成 `anthropic`(只在 providers 层 import,import-linter 保证)。
- **本地、key 不出本机、零遥测**:只发 `api.anthropic.com`(探测/起草)+ 被引第三方页(核验);对开发者服务器零字节。

GUI 不发明新概念,是把已锁定的 **SKILL 7 阶段**做成可视化 3 步;对话里的 Claude 起草由**程序化起草器**替代(这恰好落地 setup-flow-spec §4「未来可硬化:独立的、不知道焦点是谁的起草调用」)。

### GUI ↔ 现有 SKILL 阶段映射

| GUI | = SKILL 阶段 |
|---|---|
| 首页(简介 + 用法 + key 安全保证) | ≈ README |
| Step 1 品牌信息 | 阶段 0 收集输入 + 阶段 1 竞品 |
| Step 2 选档 + 起草 + 审改 | 阶段 2–4(起草器替代对话 Claude) |
| Step 3 设置 + 跑 | 阶段 5–6 冻结 + run + estimate |
| HTML report | Phase 1 已有的 `render_html` |

---

## 1. 向导流程(首页 + 3 步 + 报告)

```
首页(工具简介 · 使用方法 · API key 安全讲解与保证)
  → Step1 品牌全名 / 域名 / 品类(必填) + 可选(缩写·别名·竞品)
  → Step2 选起草档(免费 / AI 两档,默认免费)→ 起草[AI 档在此收 key] → 可编辑表格审改(免费档锁品牌/品类令牌;Local 标黄)
  → Step3 预算 + estimate 成本显示 [免费档在此收 key] → 「开始」→ 异步 run ~25min
  → 自动打开 HTML report
```

### 首页
工具是什么 · 怎么用 · **API key 安全保证**:本地输入/不落盘(Phase 3 上系统钥匙串)/不进日志/不进命令行参数/只发 `api.anthropic.com`/零遥测/开源可审计。一个「开始」按钮。

### Step 1 · 品牌信息
- 必填:焦点品牌**全名** · 焦点**域名** · **品类/行业描述**(驱动起草的唯一输入,不可省)
- 可选:缩写/别名(`aliases`)· 歧义简称(`ambiguous_aliases`,记录不计分)· 种子竞品
- 高级(可折叠,有默认):locale/region/模型档位(默认 en-US / US / standard)

### Step 2 · 选档 + 起草 + 审改(核心新功能)
顶部**两档单选**(默认**免费**;AI 档标注"质量最好,约 $0.2"):

| | 中立 20 题 | 防御 4 题 | 竞品建议 | 起草成本 | 起草需 key? |
|---|---|---|---|---|---|
| **免费档(模板)** | 固定模板,`{category}` **锁定令牌** | 固定模板,`{brand}` **锁定令牌** | 手填 | **$0** | 否 |
| **AI 档(全 AI 盲起草)** | **AI 真盲起草**(不传焦点名) | **AI 起草**(注入焦点名) | AI 建议 | ~$0.2 | 是 |

- **免费档**:品类 `{category}` 与品牌 `{brand}` 都是**锁定令牌**——Step 1 填一次,自动注入所有题,表格里显示成**只读芯片**、不可直接改;改它们只能回 Step 1(见 §1 单一输入源)。句子其余措辞仍可编辑。
- **AI 档**:选了 → 本步收 key(起草要调 API)→ 点「起草」→ 后台:中立 20(真盲,不传焦点名)+ 防御 4(注入焦点名)+ 竞品建议 → `validate(draft)` 自修闭环 → 返回 24 题。AI 把品牌/品类自然写进句子(不强制令牌),用户在表格自由改、validate 兜底。
- **可编辑表格**(按桶分组),实时 `validate(final)`:🟡 告警黄 · 🔴 红线大声警告但**不拦冻结**(D2);**Local/场景 6 题在免费档标黄**提示"模板较泛,建议手改得更贴行业"(起草规则书 §4)。
- 用户改 / 拍板。

### Step 3 · 设置 + 跑
- 预算 `budget_cap_usd`(普通 vs 烧钱)· 可选别名补充 · **estimate 成本显示**(调 `estimate`,免费离线,显示"约 $X / 上界")
- **若还没收过 key(免费档一路到此)→ 本步收 key**
- 「开始」→ 冻结 config(版本 `<name>-YYYY.MM-vN`)+ subprocess `python -m aivis.cli run --provider anthropic --yes`(key 走 **env**、不走命令行)→ 异步 ~25min,轮询日志显示阶段级进度。

### 报告
跑完自动打开本机 `report.html`(Phase 1 的 `render_html`)。

### key 懒收集规则(🟥)
key 在**第一次真正需要时**才收:AI 档在 Step 2(起草要调 API);免费档在 Step 3(跑前)。**不管哪档,真测量 run 永远要 key。** 好处:免费档**连 key 都不用就能起草+预览 24 题**——本身是隐私卖点。

### 🟥🟥 单一输入源:一次填写,处处自动引用(优先级第一)
用户在 **Step 1 只填一次** 品牌全名 / 域名 / 品类 / 缩写别名 / 竞品,这是**唯一输入源(single source of truth)**,自动流向所有下游,**绝不要求重复填写同一内容**:

- **起草**:品类自动填进 20 中立题;焦点品牌**全名**自动注入 4 防御题(templater/drafter 已如此实现,见 templater.py)。
- **竞品**:Step 1 手填的 + AI 建议的合并成**一份**,后续检测、防御题校验、SOV 全引用它,不重输。
- **冻结**:`brands.yml`(focal + competitors)与 `questions.yml` 由这份输入**自动生成**,用户不再重打品牌名/品类。
- **estimate / run / report**:全部读**同一份**冻结配置,无二次输入。

**实现**:GUI 维护一个 `ProjectInputs` 状态对象(Step 1 收集一次),各步骤只读它;任何修改只在 Step 1 一处,改了自动重新传导(重起草 / 重冻结)。后端库函数(templater / drafter / scaffold / estimate / run)都以**参数**接收这些字段,天然支持"填一次、传到底"——GUI 只需把 `ProjectInputs` 的字段分发下去,不让用户重敲。

---

## 2. 品牌数与 HTML 通用性(已核实)

- **HTML 完全数据驱动**:`render_html` 遍历 `bundle.brands`/`ranked`,无写死的 5;单品牌已守卫(`gap` 仅 `len>1`、SOV 甜甜圈仅 `len(segs)>=2` 否则回退文字);1~5 品牌均正常渲染(report.py)。
- **🟦 上限 ≤5(含焦点)**:**在 GUI 向导里硬限制** + configcheck 加一条 WARN;**引擎不设硬 cap**(通用测量框架,§2 契约不为产品决策强加上限)。
- **重要认知**:**品牌数不影响 API 成本**——成本 = 题数 × 轮数;竞品由 `extract`(纯)从同一批回答检测,加竞品运行时零额外花费。5 的上限是**可读性**(调色板 8 色、甜甜圈别太碎),不是成本控制。
- **小优化(可选)**:单品牌时隐掉"Highest/Lowest"冗余对比,做个单品牌专用排版。

---

## 3. 竞品来源

- **手填:✅ 已支持**(`Brand` 契约 + brands.yml + `competitors_on`)。
- **AI research 推荐:❌ 需新建**——`providers/drafter.py` 加一个 web_search 调用(AI 档),按品类提竞品名+域名,用户增/删/确认。对齐 SKILL 阶段 1。诚实:漏竞品 → SOV 标注"在声明的 N 个内",跑后中立题自审计漏掉的(下一基线补)。

---

## 4. 技术选型

| 决策 | 选择 | 理由 |
|---|---|---|
| 本地网页 | 标准库 `http.server`(`ThreadingHTTPServer`)+ 单 `index.html` + 原生 JS | §3 零新依赖;3 步向导只需几个 JSON 端点 |
| 异步(~25min) | subprocess 调 `python -m aivis.cli run`,日志文件 + 轮询端点 | 引擎当黑盒、隔离崩溃、复用全部成本闸门;引擎内 `budget_cap` 运行时硬封顶仍在 |
| 并发 | 单任务(单用户本地工具),状态存内存 +(可选)`status.json` 供刷新恢复 | 简单够用 |
| key 存储 | **Phase 2 只在内存、传 env、不落盘**;keyring 留 Phase 3 | keyring 是新依赖(§3),Phase 2 不持久化最安全、零依赖 |
| 起草成本确认 | 显式「起草」按钮触发,标注"约 $X、用你的 key" | 大 run 仍走自己的 estimate + budget_cap |

### 本地安全(作品集加分)
- 严格绑 `127.0.0.1`;**校验 Host 头 + 每会话随机 token**,挡本地 CSRF/DNS-rebinding(分发文档原把这条列为"暂不做"的已知边界,这里顺手补)。
- key 永不落盘 / 不进日志 / 不进命令行(走 env 传子进程,命令行在进程列表可见)。
- 抓取的第三方页沿用现有 SSRF 防护 + HTML 消毒(prompt-injection-safe)。

---

## 5. 架构落点 + import-linter

| 新东西 | 放哪 | 性质 |
|---|---|---|
| 确定性模板填充(免费档,品类/品牌锁定令牌) | `src/aivis/templater.py` | **纯函数,零 I/O 零网络** → 可配黄金测试、白嫖 CI |
| AI 起草 + 竞品建议(AI 档) | `src/aivis/providers/drafter.py` | I/O,import `anthropic`(providers 层合规) |
| 本地向导服务 | `src/aivis/gui/`(server.py + index.html) | 外壳,stdlib only |
| `gui` 子命令 | `cli.py` 一行转发到 `aivis.gui.serve()` | 薄 |

- 起草/审核/冻结/run **共用同一条路径**;档位只改"题从哪来"。
- 冻结复用 `scaffold.build_scaffold/write_scaffold` + `configio`;质检复用 `configcheck.validate_config`。
- import-linter 保持绿;`templater` 纯层不碰网络/SDK;`drafter` 的 anthropic import 只在 providers 层。

---

## 6. 实现分块(小步、各自可测、mock 免费验证)

| 步 | 内容 | 测试 |
|---|---|---|
| **2.1** | `templater.py`(纯模板填充 24 题)+ `providers/drafter.py`(盲起草20 + 防御4 + 竞品建议 + JSON 解析 + 有界自修) | 模板配黄金;drafter 用假 anthropic client 单测,不真调 |
| **2.2** | `gui/server.py` 骨架(127.0.0.1 + Host 守卫 + token)+ `cli gui` 子命令 + 静态壳 | 起服务 / 拒坏 Host |
| **2.3** | 端点接线:起草 / validate / 冻结(scaffold)/ estimate / subprocess run + 状态轮询 + ≤5 限制 | 端点单测 |
| **2.4** | 3 步向导 index.html(原生 JS,淡橙风,可编辑表格 + 实时 validate + 异步进度 + 单品牌排版) | 手测 + 快照 |
| **2.5** | mock 全程 e2e(免费)+ README/记忆同步 + 五检查员绿 | e2e 入 CI |

**起草 prompt 是测量诚实最敏感的一块**——从 question-drafting-principles.md 转写,写好后**先给用户过一遍**再依赖它。

---

## 7. 诚实与红线约束(对齐 CLAUDE.md §0/§7)

- AI 起草是 SETUP 一次性动作,**绝不进测量轨、绝不每次运行重生成**(否则基线断裂)。
- AI 只起草,**人最终拍板**;破红线只大声警告、不替用户拒绝(D2)。
- 盲起草:中立 20 题用全新不含焦点的调用(真盲);防御 4 题单独注入焦点名。
- 报告/文案统一口径"Claude API web-search 可见度(代理测量)",不说"AI 排名真相",不承诺"照做就提升"。

---

## 8. 后续(不在 Phase 2)

- **Phase 3**:`keyring` 系统钥匙串存 key("记住 key")+ 报告"本次网络去向"面板 + 明确"无遥测"声明。
- **Phase 4**:Windows 安装包(先 Python 打包,非 Electron/Tauri)/ 代码签名 / 可复现构建 / 发布说明。
- 品类模板库(C)作为免费档的行业化缓存层(让免费档的 Local 桶也贴行业);序贯采样;driver tags。
