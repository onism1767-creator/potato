<div align="center">

<img src="docs/assets/potato-banner.svg" alt="Potato — Easy AI visibility check" width="760">

# 🥔 Potato

[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![python](https://img.shields.io/badge/python-3.12%2B-blue)](pyproject.toml)
[![tests](https://img.shields.io/badge/tests-254%20passing-brightgreen)](tests/)
[![runs locally](https://img.shields.io/badge/runs-100%25%20local-success)](#4--bring-your-own-api-key)
[![cost](https://img.shields.io/badge/author%20fee-%240-success)](#what-it-costs)

[English](README.md) · [中文](README.zh.md)

</div>

> Not a crawler, not an "AI-ranking truth detector", not a recommendation engine.
> Potato is a reproducible **proxy measurement** — every result represents **only this engine, under this exact config**.

---

## 1 · What Potato is

Potato measures how visible your brand is **inside Claude's web-search answers**. It asks Claude a fixed set of frozen questions, collects every brand **mention** and source **citation**, then scores them with **deterministic rules — no AI judge, no guesswork**. The result is a reproducible, auditable report where **every number carries a confidence interval**.

---

## 2 · How to use it (Windows · no Python needed)

1. Download `AI-Visibility-Easy-Tool-windows.zip` from the **Releases** page (right-hand side of this repo).
2. **Unzip the whole folder first** — don't run it from inside the `.zip`.
3. Double-click **`AI-Visibility-Easy-Tool.bat`**.
4. A small black console window opens and your browser launches the wizard automatically. *(Keep that window open while you use the tool; close it to stop.)*
5. Follow the **3 steps** on the page:
   - **Brand** — your brand name, official domain, and category *(optional: aliases, competitors)*.
   - **Draft & review** — generate the question set (a **free template = $0, no key**, or **AI-drafted** with your own key), edit anything you like, and run the built-in quality check.
   - **Run & open report** — choose the **free mock preview ($0)** or a **real run against Claude**, then open the HTML report with one click.

<p align="center"><img src="docs/assets/gui-wizard.png" alt="Potato local wizard — Step 1: brand & category" width="660"></p>

The portable build ships an **official, python.org-signed Python** (not a homemade `.exe`), so Windows **Smart App Control won't block it** — no code-signing certificate needed. The free preview needs no key and costs nothing.

<sub>**Developers / macOS / Linux:** `pip install -e ".[dev]"`, then `aivis gui` (wizard) or `aivis run --yes` ($0 mock). Build the portable zip with `python tools/build_portable.py`. *(The project is named Potato; the CLI command and Python package are `aivis`.)*</sub>

---

## 3 · What you get — and how it stays honest

<p align="center"><img src="docs/assets/report-multi-brand.png" alt="Potato HTML report — multi-brand dashboard with ranking and share-of-voice" width="780"></p>

<sub>A single-file, offline HTML report (no external links, no scripts). Above: a $0 mock demo with 5 neutral brands. A live example: <a href="examples/single_brand_report.html">examples/single_brand_report.html</a>.</sub>

### What it measures

- **Mention coverage** — how often an answer names your brand by a clear name.
- **Citation validity** — share of cited links that are actually reachable.
- **Owned vs earned citations** — cited from your own domain vs third-party pages (counted **separately**, never blended).
- **Share of voice** — your share of all mentions across the measured brands.
- **Stability** — how consistent the result is across repeated rounds.
- **Prominence** — how early / prominently your brand shows up.
- **Presence matrix** — for each question, the strongest evidence reached: *mentioned → cited → verified*.
- **Evidence chain** — every cited URL: owned or earned, reachable or not, whether the page really mentions the brand, and the check method.
- A **ranking** with Wilson 95% confidence intervals, plus a **health light**.

### How it refuses to fabricate facts (the technical core)

- **Deterministic scoring, no AI judge.** Every counted number comes from fixed rules, not a model's opinion. AI is used only to *draft* questions (which you approve and freeze) and to *narrate* the report — **never to decide the metrics**.
- **Conservative counting.** Only a clear, unambiguous brand name scores. Abbreviations and ambiguous names are recorded but **not counted** — the tool never rounds up in your favor.
- **Three evidence tiers — `mentioned → cited → verified` — and only `verified` enters the core score.** "Verified" means Potato actually fetched the cited page and confirmed it is reachable **and really mentions the brand**.
- **Independent citation check.** Potato re-fetches the third-party pages Claude cited and checks them itself (dead links, mismatches) — it does **not** take Claude's word for them.
- **Clean room.** Every probe is a fresh call with no history and no personalization; region, language and temperature are pinned — the same question under the same conditions, every time.
- **Honest uncertainty.** Every ratio is shown with a Wilson 95% interval. When two brands' intervals overlap, Potato says **"not distinguishable"** instead of inventing a precise ranking.
- **No leading the witness.** 24 frozen questions (8 discovery / 6 comparison / 6 scenario / 4 brand-defense); the **first 20 never name any brand**, and your own brand is drafted **blind** — the AI is never told which brand is yours.
- **We pinned the search tool that actually returns citations.** A newer web-search tool version silently dropped inline citations — we caught it empirically (**0 vs 12** citations) and pinned the version that keeps them.
- **Injection-safe.** Fetched web pages and AI answers are treated as untrusted data — never fed back to the model as instructions; HTML is sanitized.

### Regional categories → one national baseline

Some categories are answered differently depending on location. Potato **pins a single region and language** (e.g. US / en-US) and runs every probe clean-room, so a run is one consistent **national-level** view — not a blur of different cities, and not your personalized results. Different regions are measured as **separate strata** and are **never averaged together** into a single misleading number.

### Today it measures Claude — but it is modular

Right now Potato connects to **Claude (with web search) only**, so what you see is **your visibility on Claude specifically** — not "AI in general." But the engine is **provider-agnostic by design**: every model sits behind one adapter interface, with no model-specific branches in the pipeline. Other AIs can be added later — or **you can write your own adapter** — **without rewriting the engine**.

---

## 4 · Bring your own API key

### What the key is for

- The **free preview (mock) needs no key and costs nothing.**
- A key is needed only for the two steps that actually call Claude: **AI question drafting** (optional) and a **real measurement**.
- You bring your **own Anthropic API key**, and **Anthropic bills you directly**.

### What it costs

- **The author charges you nothing — ever. Potato is open source (MIT).**
- A full real check is **hard-capped to a budget you set** — on the **economy (Haiku) tier it typically lands around $5 or less**, with higher-quality tiers (Sonnet / Opus) costing more. Potato estimates the cost before it starts and **stops before it can overrun**. That money is Claude usage paid to **Anthropic**, never to the author.

### Security (verifiable, not just a promise)

- Runs **only on your machine** (`127.0.0.1`). There is **no cloud server, and nothing is uploaded.**
- Your key lives **in memory only**, passed to a local subprocess through an environment variable — **never written to disk, never logged, never placed on a command line.**
- It is sent **only to `api.anthropic.com`** (your own account) — **never to any author server. Zero telemetry.**
- Open source and auditable; the report's **"network destinations"** panel shows exactly where the run connected, so you can verify all of the above yourself.

---

### For developers & advanced use

- **Measure your own brand from the CLI / Claude Skill** (SETUP mode): `aivis init` scaffolds a focal config → `aivis validate` checks it → `aivis estimate` prices a run → `aivis run` executes it. See [SKILL.md](SKILL.md).
- **Project map:** [SKILL.md](SKILL.md) · [ARCHITECTURE.md](ARCHITECTURE.md) · [CLAUDE.md](CLAUDE.md) (behavioral red lines) · `src/aivis/contracts/` (data contracts) · `configs/demo/` (neutral 5-brand demo).
- **Quality bar:** contracts frozen, three-layer storage (raw is append-only), deterministic pure scoring, golden tests, CI guards (ruff / mypy / import-linter / gitleaks / pytest) — **254 tests green.**

## License

[MIT](LICENSE).
