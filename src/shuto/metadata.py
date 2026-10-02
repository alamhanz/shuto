"""Generate Shutterstock metadata for a photo with Claude on Amazon Bedrock."""

from __future__ import annotations

import io
import json
import re
from dataclasses import asdict, dataclass, field

from .categories import SHUTTERSTOCK_CATEGORIES, normalize_categories

MAX_KEYWORDS = 50
MIN_KEYWORDS = 7  # Shutterstock requires at least 7
MAX_DESCRIPTION_CHARS = 200
PREVIEW_MAX_SIDE = 1568  # good quality/cost trade-off for Claude vision

PROMPT = f"""You are a stock-photo metadata expert writing for Shutterstock buyers.
Look at the photo and return ONLY a JSON object with these fields:

- "description": one natural English sentence (max {MAX_DESCRIPTION_CHARS} chars) that a
  buyer would search for. Concrete subject, setting, mood. No keyword stuffing.
  If the location is clearly identifiable (e.g. a landmark in Japan), name it.
- "keywords": 25-{MAX_KEYWORDS} lowercase English keywords/short phrases, most relevant first.
  Include subject, setting, concepts, colors, mood, and location where evident.
  No brand names, no trademarks, no people's names.
- "categories": 1 or 2 values chosen ONLY from: {", ".join(SHUTTERSTOCK_CATEGORIES)}
- "editorial": true if the image shows recognizable people without a model release,
  visible logos/brands/trademarks, or newsworthy events; otherwise false.
- "mature": true only for nudity, violence, or other mature content.
- "illustration": false (these are photographs).

{{hints}}Return JSON only, no prose."""


@dataclass
class PhotoMetadata:
    filename: str
    description: str
    keywords: list[str]
    categories: list[str]
    editorial: bool = False
    mature: bool = False
    illustration: bool = False
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def make_preview(image_bytes: bytes, max_side: int = PREVIEW_MAX_SIDE) -> bytes:
    """Downscale to a JPEG preview so the Bedrock request stays small and cheap."""
    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(image_bytes)) as im:
        im = ImageOps.exif_transpose(im)
        im.thumbnail((max_side, max_side))
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=85)
        return buf.getvalue()


def _clean_keywords(raw) -> list[str]:
    if isinstance(raw, str):
        raw = raw.split(",")
    out: list[str] = []
    seen: set[str] = set()
    for kw in raw or []:
        k = re.sub(r"\s+", " ", str(kw)).strip().strip('"').lower()
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out[:MAX_KEYWORDS]


def _extract_json(text: str) -> dict:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"Model did not return JSON: {text[:200]!r}")
    return json.loads(match.group(0))


def parse_model_output(filename: str, text: str) -> PhotoMetadata:
    data = _extract_json(text)
    warnings: list[str] = []

    description = re.sub(r"\s+", " ", str(data.get("description", ""))).strip()
    if len(description) > MAX_DESCRIPTION_CHARS:
        description = description[:MAX_DESCRIPTION_CHARS].rsplit(" ", 1)[0]
        warnings.append("description truncated")
    if not description:
        warnings.append("empty description")

    keywords = _clean_keywords(data.get("keywords"))
    if len(keywords) < MIN_KEYWORDS:
        warnings.append(f"only {len(keywords)} keywords (min {MIN_KEYWORDS})")

    return PhotoMetadata(
        filename=filename,
        description=description,
        keywords=keywords,
        categories=normalize_categories(data.get("categories")),
        editorial=bool(data.get("editorial", False)),
        mature=bool(data.get("mature", False)),
        illustration=bool(data.get("illustration", False)),
        warnings=warnings,
    )


def generate_metadata(
    bedrock_client,
    model_id: str,
    filename: str,
    image_bytes: bytes,
    hints: str = "",
) -> PhotoMetadata:
    """Call Bedrock Converse with the image and return parsed metadata."""
    preview = make_preview(image_bytes)
    hint_text = f"Photographer notes: {hints}\n\n" if hints else ""
    resp = bedrock_client.converse(
        modelId=model_id,
        messages=[
            {
                "role": "user",
                "content": [
                    {"image": {"format": "jpeg", "source": {"bytes": preview}}},
                    {"text": PROMPT.replace("{hints}", hint_text)},
                ],
            }
        ],
        inferenceConfig={"maxTokens": 1500, "temperature": 0.3},
    )
    text = "".join(block.get("text", "") for block in resp["output"]["message"]["content"])
    return parse_model_output(filename, text)
