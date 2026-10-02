import io
import json
from datetime import datetime

from PIL import Image

from shuto.categories import normalize_categories
from shuto.metadata import (
    generate_metadata,
    inspect_image,
    make_preview,
    normalize,
    parse_model_output,
)


def _jpeg(w=3000, h=2000) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (120, 30, 200)).save(buf, format="JPEG")
    return buf.getvalue()


def test_parse_model_output_cleans_fields():
    text = "Here you go:\n" + json.dumps(
        {
            "description": "  Torii gates   at Fushimi Inari shrine in Kyoto, Japan ",
            "keywords": ["Torii", "torii", " Kyoto ", "japan", "shrine", "red", "travel", "asia"],
            "categories": ["buildings/landmarks", "Religion", "Nature"],
            "editorial": False,
        }
    )
    m = parse_model_output("a.jpg", text)
    assert m.description == "Torii gates at Fushimi Inari shrine in Kyoto, Japan"
    assert m.keywords[:3] == ["torii", "kyoto", "japan"]
    assert len(m.keywords) == 7
    assert m.categories == ["Buildings/Landmarks", "Religion"]
    assert m.warnings == []


def test_parse_model_output_warns_on_few_keywords_and_long_description():
    text = json.dumps({"description": "word " * 100, "keywords": "a, b", "categories": ["Bogus"]})
    m = parse_model_output("b.jpg", text)
    assert len(m.description) <= 200
    assert m.categories == ["Miscellaneous"]
    assert any("keywords" in w for w in m.warnings)
    assert "description truncated" in m.warnings


def test_normalize_categories_max_two():
    assert normalize_categories(["nature", "People", "Arts"]) == ["Nature", "People"]


def test_make_preview_downscales():
    out = make_preview(_jpeg())
    with Image.open(io.BytesIO(out)) as im:
        assert max(im.size) == 1568


def test_generate_metadata_calls_bedrock_converse():
    class FakeBedrock:
        def converse(self, **kwargs):
            self.kwargs = kwargs
            body = {
                "description": "A cat",
                "keywords": [f"k{i}" for i in range(10)],
                "categories": ["Animals/Wildlife"],
            }
            return {"output": {"message": {"content": [{"text": json.dumps(body)}]}}}

    fake = FakeBedrock()
    m = generate_metadata(
        fake,
        "jp.anthropic.claude-sonnet-4-6",
        "cat.jpg",
        _jpeg(),
        "Tokyo",
        captured=datetime(2026, 9, 20, 10, 0),
    )
    assert fake.kwargs["modelId"] == "jp.anthropic.claude-sonnet-4-6"
    content = fake.kwargs["messages"][0]["content"]
    assert content[0]["image"]["format"] == "jpeg"
    assert "Photographer notes: Tokyo" in content[1]["text"]
    assert "Capture date: September 20, 2026" in content[1]["text"]
    assert m.categories == ["Animals/Wildlife"]


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


def test_inspect_image_reads_size_and_capture_date():
    exif = Image.Exif()
    exif.get_ifd(0x8769)[0x9003] = "2026:09:20 10:11:12"
    buf = io.BytesIO()
    Image.new("RGB", (2400, 1800)).save(buf, "JPEG", exif=exif.tobytes())
    info = inspect_image(buf.getvalue())
    assert (info.width, info.height) == (2400, 1800)
    assert round(info.megapixels, 2) == 4.32
    assert info.captured == datetime(2026, 9, 20, 10, 11, 12)
