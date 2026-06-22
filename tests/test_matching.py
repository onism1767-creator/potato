"""matching 单一匹配口径单测:extract/verify/configcheck 共用,边界一致。"""

from aivis.matching import find_positions, mentions_surfaces, name_pattern


def test_case_insensitive() -> None:
    assert name_pattern("Osprey").search("I love OSPREY packs")
    assert name_pattern("osprey").search("Osprey rules")


def test_hyphen_and_space_names() -> None:
    # 含连字符/空格的名字按整体匹配(\b 对 "Co-op" 不可靠,故用自定义边界)
    assert name_pattern("REI Co-op").search("The REI Co-op bag is great")
    assert name_pattern("Co-op").search("buy Co-op gear")


def test_word_boundary_no_substring_match() -> None:
    # 不命中子串:"Deuter" 不应在 "Deuterium" 里命中
    assert not name_pattern("Deuter").search("Deuterium is an isotope")
    assert name_pattern("Deuter").search("Deuter makes packs")


def test_find_positions_sorted_and_skips_empty() -> None:
    hits = find_positions("Osprey then Gregory then Osprey", ("Osprey", "Gregory", ""))
    assert [name for _, name in hits] == ["Osprey", "Gregory", "Osprey"]
    assert [pos for pos, _ in hits] == sorted(pos for pos, _ in hits)


def test_mentions_surfaces_dedup_order() -> None:
    found = mentions_surfaces("Osprey vs Gregory, Osprey wins", ("Osprey", "Gregory", "Deuter"))
    assert found == ["Osprey", "Gregory"]  # 去重保序,未出现的不算


def test_extract_and_configcheck_agree() -> None:
    # 🟥 同一文本、同一名字:extract 用的 find_positions 与 configcheck 用的 mentions_surfaces
    # 必须口径一致(都来自 matching),不会"一个命中一个不命中"
    text = "The REI Co-op pack beats Deuterium-brand knockoffs"
    assert bool(find_positions(text, ("REI Co-op",))) == (
        "REI Co-op" in mentions_surfaces(text, ("REI Co-op",))
    )
    # "Deuter" 在 "Deuterium-brand" 里都不命中(子串保护一致)
    assert find_positions(text, ("Deuter",)) == []
    assert mentions_surfaces(text, ("Deuter",)) == []
