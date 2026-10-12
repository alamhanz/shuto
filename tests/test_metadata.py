import json

from shuto.categories import normalize_categories
from shuto.metadata import normalize


def test_normalize_cleans_fields():
    m = normalize(
        "a.jpg",
        {
            "description": "  Torii gates   at Fushimi Inari shrine in Kyoto, Japan ",
            "keywords": ["Torii", "torii", " Kyoto ", "japan", "shrine", "red", "travel", "asia"],
            "categories": ["buildings/landmarks", "Religion", "Nature"],
            "editorial": False,
        },
    )
    assert m.description == "Torii gates at Fushimi Inari shrine in Kyoto, Japan"
    assert m.keywords[:3] == ["torii", "kyoto", "japan"]
    assert len(m.keywords) == 7
    assert m.categories == ["Buildings/Landmarks", "Religion"]
    assert m.warnings == []


def test_normalize_warns_on_few_keywords_and_long_description():
    m = normalize(
        "b.jpg", json.loads(json.dumps({"description": "word " * 100, "keywords": "a, b"}))
    )
    assert len(m.description) <= 200
    assert m.categories == ["Miscellaneous"]
    assert any("keywords" in w for w in m.warnings)
    assert "description truncated" in m.warnings


def test_normalize_categories_max_two():
    assert normalize_categories(["nature", "People", "Arts"]) == ["Nature", "People"]


def test_normalize_handles_hand_edited_json():
    m = normalize(
        "x.jpg",
        {
            "description": "KYOTO, JAPAN - UNKNOWN: festival",
            "keywords": "a,b,c,d,e,f,g",
            "categories": ["Celebrities", "Holidays"],
            "editorial": "true",
            "mature": "no",
        },
    )
    assert m.editorial is True and m.mature is False
    assert m.categories == ["Holidays"]
    assert "editorial caption missing city/date" in m.warnings
    assert any("editorial caption format" in w for w in m.warnings)


def test_good_editorial_caption_has_no_format_warning():
    m = normalize(
        "x.jpg",
        {
            "description": "TOKYO, JAPAN - OCTOBER 5, 2026: Crowds cross at Shibuya.",
            "keywords": "a,b,c,d,e,f,g",
            "editorial": True,
        },
    )
    assert m.warnings == []


def test_short_commercial_title_warns():
    m = normalize("x.jpg", {"description": "Red gate", "keywords": "a,b,c,d,e,f,g"})
    assert any("under 5 words" in w for w in m.warnings)
