"""Describe stage: build the prompt, call the vision LLM, apply style rules and guardrails.

Pure module: no SQLite, no config files. The caller passes a ``Style`` and the loaded
restrictions. GPS data never reaches the model; only the capture date and the notes do.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from . import filters as filters_mod
from . import guardrails
from .categories import SHUTTERSTOCK_CATEGORIES
from .filters import FilterResult, FilterSpec
from .llm.base import LLMRequest, LLMResult, VisionLLM, describe_structured
from .llm.schema import PhotoOutput, photo_schema
from .metadata import MAX_DESCRIPTION_CHARS, PhotoMetadata, normalize

MONTHS = (
    "JANUARY FEBRUARY MARCH APRIL MAY JUNE JULY AUGUST SEPTEMBER OCTOBER NOVEMBER DECEMBER"
).split()


@dataclass
class Style:
    theme: str = ""
    tone: str = "documentary, warm, factual; no hype words"
    language: str = "en"
    region_focus: str = ""
    keywords_min: int = 25
    keywords_max: int = 50
    always_include: list[str] = field(default_factory=list)
    romaji_place_names: bool = False
    seasonal_terms: bool = True
    avoid: list[str] = field(default_factory=lambda: ["beautiful", "amazing", "stunning"])
    extra_instructions: str = ""
    default_city: str = ""
    default_country: str = ""

    @classmethod
    def from_config(cls, style: Any) -> Style:
        """Build from ``config.StyleConfig`` (duck-typed, so this module stays config-free)."""
        k = style.keywords
        focus = str(getattr(style, "region_focus", "") or "")
        return cls(
            theme=style.theme,
            tone=style.tone,
            language=style.language,
            region_focus=focus,
            keywords_min=k.min,
            keywords_max=k.max,
            always_include=list(k.always_include),
            romaji_place_names=k.romaji_place_names or focus == "japan",
            seasonal_terms=k.seasonal_terms,
            avoid=list(style.avoid),
            extra_instructions=style.extra_instructions,
            default_city=style.default_place.city,
            default_country=style.default_place.country,
        )


@dataclass
class DescribeResult:
    metadata: PhotoMetadata
    detected: dict
    location: dict
    guardrails: guardrails.GuardrailResult
    llm: LLMResult
    filters: FilterResult | None = None
    place: dict = field(default_factory=dict)


def _ascii_upper(text: str | None) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).upper()
    text = re.sub(r"[^A-Z .'/-]", " ", text)
    return re.sub(r"\s+", " ", text).strip(" .-'/")


PLACE_SOURCES = ("ai", "default", "partly default", "unknown")
NO_PLACE_WARNING = "No place known: set a default place in Settings or edit the caption"


def resolve_place(location: Any, default_city: str = "", default_country: str = "") -> dict:
    """The city and country for an editorial caption and where each came from.

    The AI's answer wins; a missing part is filled from the default place. ``source`` is
    "ai" (nothing defaulted and something known), "default" (all from the default),
    "partly default" (a mix) or "unknown" (nothing known). The photo's GPS never reaches the
    model, so there is no GPS source. Parts stay empty when still unknown.
    """
    if isinstance(location, dict):
        city, country = location.get("city"), location.get("country")
    else:
        city, country = getattr(location, "city", None), getattr(location, "country", None)
    city_s, country_s = _ascii_upper(city), _ascii_upper(country)
    from_ai = bool(city_s) + bool(country_s)
    used = 0
    if not city_s and (fill := _ascii_upper(default_city)):
        city_s, used = fill, used + 1
    if not country_s and (fill := _ascii_upper(default_country)):
        country_s, used = fill, used + 1
    if not (city_s or country_s):
        source = "unknown"
    elif used == 0:
        source = "ai"
    elif from_ai == 0:
        source = "default"
    else:
        source = "partly default"
    return {"city": city_s, "country": country_s, "source": source, "default": used > 0}


def editorial_caption(
    location: Any,
    captured: datetime | None,
    sentence: str,
    default_city: str = "",
    default_country: str = "",
) -> str:
    """`CITY, COUNTRY - MONTH D, YYYY: sentence`; UNKNOWN stands in for missing parts that
    the default place (when set) cannot fill.

    ``location`` is a dict or an object with ``city`` and ``country`` attributes.
    """
    place = resolve_place(location, default_city, default_country)
    city_s = place["city"] or "UNKNOWN"
    country_s = place["country"] or "UNKNOWN"
    if captured:
        date_s = f"{MONTHS[captured.month - 1]} {captured.day}, {captured.year}"
    else:
        date_s = "UNKNOWN"
    text = re.sub(r"\s+", " ", sentence or "").strip()
    if text:
        text = text[0].upper() + text[1:]
    return f"{city_s}, {country_s} - {date_s}: {text}"


def feedback_lines(stats: dict[str, int], examples: Iterable[dict] = ()) -> list[str]:
    """Turn recent rejection stats/examples (from the store) into prompt lines."""
    lines: list[str] = []
    if stats:
        top = sorted(stats.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
        lines.append(
            "Recent Shutterstock rejections by reason: "
            + ", ".join(f"{code} x{n}" for code, n in top)
            + "."
        )
    for ex in examples:
        title = str(ex.get("title") or ex.get("orig_name") or "a photo")[:120]
        line = f'A past submission "{title}" was rejected: {ex.get("reason_code", "other")}'
        if ex.get("note"):
            line += f" ({str(ex['note'])[:160]})"
        lines.append(line + ".")
    return lines


def build_prompt(
    style: Style,
    restrictions_summary: str = "",
    feedback: Iterable[str] = (),
    notes: str = "",
    captured: datetime | None = None,
    filter_lines: Iterable[str] = (),
) -> tuple[str, str]:
    """Return (system, user). The JSON shape is also spelled out for weaker local models."""
    system = (
        "You are a stock-photo metadata expert writing for Shutterstock buyers. "
        "You look at one photo and answer with a single JSON object and nothing else."
    )
    kw_rules = [
        f"{style.keywords_min}-{style.keywords_max} lowercase keywords or short phrases, "
        "most relevant first (subject, setting, concepts, colors, mood, place).",
        "No brand names, trademarks or people's names.",
    ]
    if style.romaji_place_names:
        kw_rules.append("Use romaji for Japanese place names (e.g. kyoto, shibuya).")
    if style.seasonal_terms:
        kw_rules.append("Add the season and time of day when evident.")
    if style.always_include:
        kw_rules.append("Always include: " + ", ".join(style.always_include) + ".")
    parts = [
        " ".join(
            bit
            for bit in (
                f'Portfolio theme: "{style.theme}".' if style.theme else "",
                f"Tone: {style.tone}." if style.tone else "",
                f"Language: {style.language}.",
            )
            if bit
        ),
        "",
        "Return a JSON object with exactly these fields:",
        f'- "description": one natural sentence, at most {MAX_DESCRIPTION_CHARS} characters, '
        "that a buyer would search for. Concrete subject, setting, mood. At least 5 words. "
        "No keyword stuffing.",
        '- "caption_sentence": one factual sentence saying what is happening, in the style of '
        "a news caption (used only if the photo ends up editorial).",
        '- "keywords": a list of strings. ' + " ".join(kw_rules),
        '- "categories": a list of 1 or 2 values chosen only from: '
        + ", ".join(SHUTTERSTOCK_CATEGORIES)
        + ".",
        '- "editorial": true if recognizable people (assume no model release), visible '
        "logos, brands or trademarks, or a newsworthy event are shown; otherwise false.",
        '- "mature": true only for nudity, violence or other mature content.',
        '- "detected": an object with "recognizable_people" (integer count), '
        '"logos_or_brands" (list of names, empty if none), "landmarks" (list), '
        '"artworks_or_characters" (list) and "private_interior" (true or false).',
        '- "location": an object with "city", "region" and "country" in English, '
        "or null for each one that is not clearly identifiable.",
    ]
    if style.avoid:
        parts += ["", "Never use these words: " + ", ".join(style.avoid) + "."]
    if style.extra_instructions.strip():
        parts += ["", style.extra_instructions.strip()]
    if restrictions_summary.strip():
        parts += [
            "",
            'Shutterstock restrictions. Report anything below under "detected" and set '
            '"editorial" accordingly; never put restricted names in keywords:',
            restrictions_summary.strip(),
        ]
    fb = [f for f in feedback if f]
    if fb:
        parts += ["", "Lessons from earlier submissions:"] + [f"- {f}" for f in fb]
    fl = [line for line in filter_lines if line]
    if fl:
        parts += [""] + fl
    ctx = []
    if captured:
        ctx.append(f"Capture date: {captured:%B} {captured.day}, {captured.year}")
    if notes.strip():
        ctx.append(f"Photographer notes: {notes.strip()}")
    if ctx:
        parts += [""] + ctx
    parts += ["", "Return JSON only, no prose and no code fences."]
    return system, "\n".join(parts)


def _apply_style_keywords(keywords: list[str], style: Style) -> list[str]:
    avoid = {a.strip().lower() for a in style.avoid if a.strip()}
    out: list[str] = []
    for kw in keywords:
        words = set(re.findall(r"[\w']+", kw.lower()))
        if avoid and (words & avoid):
            continue
        out.append(kw)
    have = {k.lower() for k in out}
    extra: list[str] = []
    for kw in style.always_include:
        k = kw.strip().lower()
        if k and k not in have:
            extra.append(k)
            have.add(k)
    mx = max(1, style.keywords_max)
    if len(out) + len(extra) > mx:
        out = out[: max(0, mx - len(extra))]
    return out + extra


def describe_photo(
    llm: VisionLLM,
    image_jpeg: bytes,
    filename: str,
    *,
    style: Style,
    restrictions: guardrails.Restrictions,
    notes: str = "",
    captured: datetime | None = None,
    feedback: Iterable[str] = (),
    max_tokens: int = 1500,
    temperature: float = 0.2,
    retries: int = 1,
    timeout_s: float = 300,
    has_model_release: bool = False,
    has_property_release: bool = False,
    filters: FilterSpec | None = None,
    at: str = "",
) -> DescribeResult:
    system, user = build_prompt(
        style,
        guardrails.prompt_summary(restrictions, style.region_focus or None),
        feedback,
        notes,
        captured,
        filters_mod.prompt_lines(filters) if filters else (),
    )
    schema = None
    if filters and filters.active:
        schema = photo_schema(*filters_mod.schema_properties(filters))
        max_tokens += 60 * (len(filters.rules) + int(filters.theme_active))
    req = LLMRequest(
        image_jpeg=image_jpeg,
        system=system,
        prompt=user,
        max_tokens=max_tokens,
        temperature=temperature,
        timeout_s=timeout_s,
    )
    if schema is None:
        result = describe_structured(llm, req, retries=retries)
    else:
        result = describe_structured(llm, req, retries=retries, schema=schema)
    out = PhotoOutput.model_validate(result.data)
    location = out.location.model_dump()
    detected = out.detected.model_dump()
    sentence = out.caption_sentence.strip() or out.description

    def caption() -> str:
        return editorial_caption(
            location, captured, sentence, style.default_city, style.default_country
        )

    description = out.description
    if out.editorial:
        description = caption()
    data = {
        "description": description,
        "keywords": _apply_style_keywords(out.keywords, style),
        "categories": out.categories,
        "editorial": out.editorial,
        "mature": out.mature,
    }
    meta = normalize(filename, data)

    def run_check(m: PhotoMetadata):
        return guardrails.check(
            m,
            detected,
            restrictions,
            notes=notes,
            has_model_release=has_model_release,
            has_property_release=has_property_release,
        )

    meta, gr = run_check(meta)
    if meta.editorial and not out.editorial:
        # Guardrails forced editorial: the description must become an editorial caption.
        data["description"] = caption()
        data["editorial"] = True
        data["keywords"] = meta.keywords
        meta, gr2 = run_check(normalize(filename, data))
        gr2.removed_keywords = list(dict.fromkeys(gr.removed_keywords + gr2.removed_keywords))
        gr2.hits = [h for h in gr.hits if h.kind == "trademark_term" and h.where == "keyword"] + [
            h for h in gr2.hits if not (h.kind == "trademark_term" and h.where == "keyword")
        ]
        gr2.messages = [m for m in gr.messages if m.startswith("Removed trademarked")] + [
            m for m in gr2.messages if not m.startswith("Removed trademarked")
        ]
        gr = gr2
    meta.warnings.extend(m for m in gr.messages if m not in meta.warnings)
    place = resolve_place(location, style.default_city, style.default_country)
    if meta.editorial:
        if not (place["city"] and place["country"]) and NO_PLACE_WARNING not in meta.warnings:
            meta.warnings.append(NO_PLACE_WARNING)
    else:  # the default place only ever goes into editorial captions
        place = resolve_place(location)
    checked = None
    if filters and filters.active:
        checked = filters_mod.evaluate(
            filters,
            out.theme_fit,
            out.checks,
            model=f"{llm.name}/{llm.model}",
            at=at,
        )
    return DescribeResult(meta, detected, location, gr, result, checked, place)
