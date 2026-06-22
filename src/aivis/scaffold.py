"""scaffold —— 生成焦点模式配置骨架(aivis init 用)。

build_scaffold 纯字符串构建(可测、不碰磁盘);write_scaffold 落盘(不覆盖已存在目录)。
产物开箱即过 load_frozen_config + validate --phase final(0 硬错):配比 8/6/6/4、
中立题占位不点名品牌、4 道防御题已注入焦点品牌名、焦点不变量满足。
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class ScaffoldFiles:
    questions_yaml: str
    brands_yaml: str


def _yaml(data: dict[str, Any]) -> str:
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)


# (id 前缀, intent, 题数, 中文标签, 中文提示)。🟥 占位文本刻意**不插入**用户给的
# 品类/品牌字符串、也**不含英文意图词**:否则若品类描述里带了焦点名(如品类"Acme CRM"),
# 或品牌恰名为 "TODO"/"Discovery",中立题就会"点名品牌"而误触反偏置硬错。
# 品类提示改放文件头注释(不参与校验)。
_NEUTRAL_SPECS = (
    ("d", "discovery", 8, "发现题", "漏斗顶端问题"),
    ("c", "comparison", 6, "比较题", "横向比较各家"),
    ("l", "local", 6, "本地题", "一个真实具体的使用场景"),
)


def _neutral_questions() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for prefix, intent, n, label, hint in _NEUTRAL_SPECS:
        for i in range(1, n + 1):
            out.append(
                {
                    "id": f"{prefix}{i:02d}",
                    "intent": intent,
                    "text": f"待填({label} #{i}):{hint},勿点名品牌",
                }
            )
    return out


def _defense_questions(focal_name: str, category: str | None) -> list[dict[str, Any]]:
    brand_label = f"{category} brand" if category else "brand"
    cat_prefix = f"{category} " if category else ""
    texts = (
        f"Is {focal_name} a good {brand_label}? What are its strengths and weaknesses?",
        f"How does {focal_name} compare to other {cat_prefix}brands in terms of quality?",
        f"What do users think about {focal_name}?",
        f"Are {focal_name} products worth it?",
    )
    return [
        {"id": f"b{i:02d}", "intent": "brand_defense", "text": t}
        for i, t in enumerate(texts, start=1)
    ]


def build_scaffold(
    *,
    name: str,
    focal_name: str,
    focal_domain: str,
    category: str | None = None,
    date_tag: str,
) -> ScaffoldFiles:
    """构建 questions.yml + brands.yml 文本(纯函数,不碰磁盘)。"""
    questions = _neutral_questions() + _defense_questions(focal_name, category)

    questions_data: dict[str, Any] = {
        "version": f"{name}-{date_tag}-v1",
        "prompt_version": f"{name}-questions-v1",
        "tier": "standard",
        "locale": "en-US",
        "region": "US",
        "temperature": 0.0,
        "n_runs": 3,
        "max_uses": 3,
        "budget_cap_usd": 5.0,
        "strata": ["region", "locale", "tier"],
        "questions": questions,
    }

    brands_data: dict[str, Any] = {
        "competitor_set_version": f"{name}-brands-v1",
        "focal_domain": focal_domain,
        "competitors_on": True,
        "peers": [
            {
                "name": focal_name,
                "aliases": [],
                "ambiguous_aliases": [],
                "domains": [focal_domain],
                "is_focal": True,
            }
        ],
    }

    cat_line = f"# 品类: {category}\n" if category else ""
    q_header = (
        f"# {name} 题库骨架(aivis init 生成)——配方 8 发现 + 6 比较 + 6 本地 + 4 防御 = 24。\n"
        f"{cat_line}"
        "# 🟥 前 20 题(发现/比较/本地)绝不点名品牌;只有 4 道防御题点焦点品牌名。\n"
        "# 把'待填'占位换成真实问题,然后跑: aivis validate --config-dir configs/<name>\n"
        "# 改题库 = prompt_version bump(新基线)。\n\n"
    )
    b_header = (
        f"# {name} 品牌集骨架(aivis init 生成)。focal = 你自己;competitors 自行补全。\n"
        "# 保守计数:name + aliases 计分;ambiguous_aliases 命中只记录不计分。\n"
        "# 改品牌集 = competitor_set_version bump。\n\n"
    )
    b_footer = (
        "\n# 补竞品示例(复制到 peers 下,去掉注释):\n"
        '#   - name: "Competitor Inc"\n'
        "#     aliases: []\n"
        "#     ambiguous_aliases: []\n"
        '#     domains: ["competitor.com"]\n'
        "#     is_focal: false\n"
    )

    return ScaffoldFiles(
        questions_yaml=q_header + _yaml(questions_data),
        brands_yaml=b_header + _yaml(brands_data) + b_footer,
    )


def write_scaffold(config_dir: Path, files: ScaffoldFiles) -> None:
    """写 questions.yml + brands.yml;目录已存在则拒绝(防覆盖)。"""
    if config_dir.exists():
        raise FileExistsError(f"目标目录已存在,拒绝覆盖: {config_dir}")
    config_dir.mkdir(parents=True)
    (config_dir / "questions.yml").write_text(files.questions_yaml, encoding="utf-8")
    (config_dir / "brands.yml").write_text(files.brands_yaml, encoding="utf-8")
