from shuto import metadata_rules as rules
from shuto.metadata_rules import BatchSeen, Limits

GOOD = "A quiet residential street in Tokyo with parked bicycles and utility poles"


def codes(issues):
    return {i.code for i in issues}


def test_tidy_text_strips_emoji_and_curly_quotes():
    text, changed = rules.tidy_text("A “quiet” street \U0001f338 in Tokyo—at dawn")
    assert text == 'A "quiet" street in Tokyo-at dawn'
    assert changed
    same, changed = rules.tidy_text("A quiet street.")
    assert same == "A quiet street." and not changed


def test_description_checks():
    assert codes(rules.check_description(GOOD)) == set()
    assert "too_few_words" in codes(rules.check_description("Quiet street"))
    assert "too_long" in codes(rules.check_description("word " * 50 + "end"))
    assert "comma_list" in codes(rules.check_description("street, bicycle, pole, wires, city"))
    assert "repeated_words" in codes(rules.check_description(GOOD + " and another street"))
    short = rules.check_description("A quiet street at dawn")
    assert codes(short) == {"short_description"} and not short[0].hard
    long_ok = "A quiet street in the old town " + "x" * 130
    assert "long_description" in codes(rules.check_description(long_ok))


def test_editorial_prefix_counts_toward_the_hard_maximum_only():
    sentence = ("A" + " word" * 28)[:141]  # inside the target range on its own
    assert "too_long" not in codes(rules.check_description(sentence, prefix_len=0))
    assert "too_long" in codes(rules.check_description(sentence, prefix_len=70))
    caption = "TOKYO, JAPAN - OCTOBER 5, 2026: A street."
    assert rules.strip_caption_prefix(caption) == "A street."


def test_clean_keywords_dedupes_and_applies_avoid_and_cap():
    raw = ["Street", "streets", "tokyo street", "street tokyo", "beautiful sky", "", "alley"]
    got = rules.clean_keywords(raw, Limits(kw_max=10), avoid=["beautiful"])
    assert got == ["street", "tokyo street", "alley"]
    capped = rules.clean_keywords([f"kw{i}" for i in range(30)], Limits(kw_max=5))
    assert len(capped) == 5


def test_always_include_is_appended_and_never_cut():
    raw = [f"kw{i}" for i in range(10)] + ["japan"]
    got = rules.clean_keywords(raw, Limits(kw_max=5), always_include=["japan", "everyday japan"])
    assert len(got) == 5
    assert "japan" in got and "everyday japan" in got
    assert got[:3] == ["kw0", "kw1", "kw2"]


def test_check_keywords_levels():
    limits = Limits()
    assert codes(rules.check_keywords(["a"] * 3, limits)) == {"few_keywords"}
    assert codes(rules.check_keywords(["a"] * 10, limits)) == {"light_keywords"}
    assert rules.check_keywords(["a"] * 30, limits) == []
    assert rules.Issue("few_keywords", "x").hard and not rules.Issue("light_keywords", "x").hard


def test_batch_seen_flags_near_duplicates_only():
    kws = [f"kw{i}" for i in range(20)]
    seen = BatchSeen()
    assert rules.check_unique(seen, GOOD, kws) == []  # nothing seen yet
    seen.add(GOOD, kws)
    assert codes(rules.check_unique(seen, GOOD, kws)) == {"dup_description", "dup_keywords"}
    other = rules.check_unique(seen, "Red torii gate beside a mossy stone lantern", ["gate", "red"])
    assert other == []
    one_off = [*kws[:-1], "new"]  # Jaccard 19/21 is above 0.85
    assert "dup_keywords" in codes(rules.check_unique(seen, "Different text here today", one_off))
    assert rules.similar_to(seen, GOOD, kws) == [GOOD]
