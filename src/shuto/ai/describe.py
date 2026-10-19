"""Describe stage: call the vision LLM, apply style rules and guardrails.

Pure module: no SQLite, no config files. The caller passes a ``Style`` and the loaded
restrictions. Coordinates never reach the model; only the capture date, the notes and the
short "photo facts" text (place names, photographer keywords) do. The prompt text lives in
``ai.prompts``, the place decision in ``places.resolve``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from shuto.ai import filters as filters_mod
from shuto.ai.filters import FilterResult, FilterSpec
from shuto.ai.prompts import (
    MONTHS,
    Style,
    build_prompt,
    repair_prompt,
    topup_prompt,
)
from shuto.ai.providers.base import LLMError, LLMRequest, LLMResult, VisionLLM, describe_structured
from shuto.ai.providers.schema import PhotoOutput, photo_schema
from shuto.metadata import guardrails, rules
from shuto.metadata.model import PhotoMetadata, normalize
from shuto.places.resolve import (
    NO_PLACE_WARNING,
    resolve_place,
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
    issues: list[rules.Issue] = field(default_factory=list)


def editorial_caption(
    location: Any,
    captured: datetime | None,
    sentence: str,
    default_city: str = "",
    default_country: str = "",
    known: dict | None = None,
) -> str:
    """`CITY, COUNTRY - MONTH D, YYYY: sentence`; UNKNOWN stands in for missing parts that
    the default place (when set) cannot fill.

    ``location`` is a dict or an object with ``city`` and ``country`` attributes; ``known`` is
    the place read from the photo file, which wins over the AI's answer.
    """
    place = resolve_place(location, default_city, default_country, known)
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


# Retry temperature: +step per model call so far, never above the cap (or the owner's own value).
TEMPERATURE_STEP = 0.15
TEMPERATURE_CAP = 0.9

# Problems that normalize() already reports in the metadata warnings.
_WARNED_ELSEWHERE = {"too_long", "few_keywords"}


@dataclass
class _Asked:
    """The first model call: the request to repeat, the prompt it carried and the answer."""

    req: LLMRequest
    prompt: str
    schema: dict | None
    result: LLMResult
    out: PhotoOutput
    condensed: bool


@dataclass
class _Answer:
    """An answer after the automatic fixes: the texts, the keywords and what is still wrong."""

    out: PhotoOutput
    desc: str
    cap: str
    kws: list[str]
    issues: list[rules.Issue]


@dataclass
class _Checker:
    """What ``_assess`` needs to judge an answer."""

    style: Style
    limits: rules.Limits
    seen: rules.BatchSeen | None
    prefix_len: int


def _ask(
    llm: VisionLLM,
    image_jpeg: bytes,
    *,
    style: Style,
    restrictions: guardrails.Restrictions,
    notes: str,
    captured: datetime | None,
    feedback: Iterable[str],
    max_tokens: int,
    temperature: float,
    retries: int,
    timeout_s: float,
    filters: FilterSpec | None,
    facts: dict | None,
    condensed: bool,
) -> _Asked:
    """Build the prompt and make the first model call."""
    system, user = build_prompt(
        style,
        guardrails.prompt_summary(restrictions, style.region_focus or None, short=condensed),
        feedback,
        notes,
        captured,
        filters_mod.prompt_lines(filters) if filters else (),
        str((facts or {}).get("hints") or ""),
        condensed=condensed,
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
    return _Asked(req, user, schema, result, PhotoOutput.model_validate(result.data), condensed)


def _assess(o: PhotoOutput, check: _Checker) -> _Answer:
    """(description, caption sentence, keywords, problems) of an answer after the fixes."""
    style, limits = check.style, check.limits
    fold = rules.ascii_fold if style.english_only else str
    desc, _ = rules.tidy_text(fold(o.description))
    cap, _ = rules.tidy_text(fold(o.caption_sentence.strip() or o.description))
    kws = rules.clean_keywords(
        [fold(str(k)) for k in o.keywords],
        limits,
        style.avoid,
        style.always_include,
    )
    active = cap if o.editorial else desc
    found = [
        *(rules.check_english(f"{desc} {cap}", kws) if style.english_only else []),
        *rules.check_description(
            active, limits, check.prefix_len if o.editorial else 0, style.english_only
        ),
        *rules.check_keywords(kws, limits),
        *rules.check_unique(check.seen, active, kws),
    ]
    return _Answer(o, desc, cap, kws, found)


def _repair_loop(
    llm: VisionLLM,
    asked: _Asked,
    answer: _Answer,
    check: _Checker,
    *,
    temperature: float,
    max_tries: int,
) -> tuple[_Answer, int]:
    """Ask again while a hard problem remains. Returns (best answer, model calls made)."""
    result, limits = asked.result, check.limits
    calls = result.retries + 1  # model calls so far (the first answer may have been re-asked)
    previous = (answer.desc, answer.cap, tuple(answer.kws))
    same_in_a_row = 0
    while calls < max_tries:
        hard = [i for i in answer.issues if i.hard]
        light = not hard and len(answer.kws) < min(limits.kw_min, limits.kw_max)
        if not hard and not light:
            break
        active = answer.cap if answer.out.editorial else answer.desc
        # every retry is a little warmer, so a small model does not repeat itself
        warmer = min(max(temperature, TEMPERATURE_CAP), temperature + TEMPERATURE_STEP * calls)
        if any(i.code.startswith("dup_") for i in hard):
            warmer = max(warmer, 0.5)
        topup = light or all(i.code == "few_keywords" for i in hard)
        if topup:
            wanted = min(max(limits.kw_min, rules.KEYWORD_FLOOR), limits.kw_max)
            missing = wanted - len(answer.kws)
            retry_prompt = topup_prompt(
                asked.prompt, active, answer.kws, max(1, missing), check.style, asked.condensed
            )
        else:
            others = rules.similar_to(check.seen, active, answer.kws)
            retry_prompt = repair_prompt(asked.prompt, active, answer.kws, hard, others)
        again_req = replace(asked.req, prompt=retry_prompt, temperature=warmer)
        result.retries += 1  # every repair call counts as one more try
        calls += 1
        try:
            again = describe_structured(
                llm,
                again_req,
                retries=0,
                **({} if asked.schema is None else {"schema": asked.schema}),
            )
        except LLMError:
            break
        result.input_tokens += again.input_tokens
        result.output_tokens += again.output_tokens
        result.seconds += again.seconds
        out2 = PhotoOutput.model_validate(again.data)
        fixed = answer.out.model_copy(
            update={
                "description": answer.desc if topup else out2.description,
                "caption_sentence": answer.cap if topup else out2.caption_sentence,
                "keywords": [*answer.kws, *out2.keywords] if topup else out2.keywords,
            }
        )
        found = _assess(fixed, check)
        latest = (found.desc, found.cap, tuple(found.kws))
        same_in_a_row = same_in_a_row + 1 if latest == previous else 0
        previous = latest
        hard2 = len([i for i in found.issues if i.hard])
        if light and len(found.kws) <= len(answer.kws):
            break  # a soft top-up that adds nothing new: stop asking
        if hard2 < len(hard) or (topup and hard2 <= len(hard)):  # keep the best answer
            answer = found
        if not hard2:
            break
        if same_in_a_row >= 2:
            break  # the same answer twice in a row, even when asked warmer: give up
    return answer, calls


def _guarded(
    data: dict,
    filename: str,
    editorial_asked: bool,
    caption_text: str,
    *,
    detected: dict,
    restrictions: guardrails.Restrictions,
    caption: Callable[[str], str],
    notes: str,
    has_model_release: bool,
    has_property_release: bool,
) -> tuple[PhotoMetadata, guardrails.GuardrailResult]:
    """Normalize the data and run the guardrails (twice when they force an editorial caption)."""

    def run_check(m: PhotoMetadata):
        return guardrails.check(
            m,
            detected,
            restrictions,
            notes=notes,
            has_model_release=has_model_release,
            has_property_release=has_property_release,
        )

    meta, gr = run_check(normalize(filename, data))
    if meta.editorial and not editorial_asked:
        # Guardrails forced editorial: the description must become an editorial caption.
        data["description"] = caption(caption_text)
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
    return meta, gr


def _finish(
    llm: VisionLLM,
    asked: _Asked,
    answer: _Answer,
    calls: int,
    *,
    filename: str,
    style: Style,
    restrictions: guardrails.Restrictions,
    notes: str,
    caption: Callable[[str], str],
    location: dict,
    detected: dict,
    known_place: dict | None,
    has_model_release: bool,
    has_property_release: bool,
    seen: rules.BatchSeen | None,
    repair: bool,
    filters: FilterSpec | None,
    at: str,
) -> DescribeResult:
    """The chosen answer becomes metadata: caption, guardrails, warnings, place, filter result."""
    out, issues = answer.out, answer.issues
    data = {
        "description": caption(answer.cap) if out.editorial else answer.desc,
        "keywords": answer.kws,
        "categories": out.categories,
        "editorial": out.editorial,
        "mature": out.mature,
    }
    meta, gr = _guarded(
        data,
        filename,
        out.editorial,
        answer.cap,
        detected=detected,
        restrictions=restrictions,
        caption=caption,
        notes=notes,
        has_model_release=has_model_release,
        has_property_release=has_property_release,
    )
    if any(i.hard for i in issues) and repair:
        tried = f"{calls} tries" if calls > 1 else "1 try"
        meta.warnings.append(f"Could not be fixed after {tried}: needs a look")
    for issue in issues:  # what is still wrong after the fixes and the repairs
        skip = issue.code in _WARNED_ELSEWHERE or (
            issue.code == "too_few_words" and not meta.editorial
        )
        if issue.hard and not skip and issue.text not in meta.warnings:
            meta.warnings.append(issue.text)
    if seen is not None:
        seen.add(answer.cap if meta.editorial else answer.desc, meta.keywords)
    place = resolve_place(location, style.default_city, style.default_country, known_place)
    if meta.editorial:
        if not (place["city"] and place["country"]) and NO_PLACE_WARNING not in meta.warnings:
            meta.warnings.append(NO_PLACE_WARNING)
    else:  # the default place only ever goes into editorial captions
        place = resolve_place(location, known=known_place)
    checked = None
    if filters and filters.active:
        checked = filters_mod.evaluate(
            filters,
            out.theme_fit,
            out.checks,
            model=f"{llm.name}/{llm.model}",
            at=at,
        )
    return DescribeResult(meta, detected, location, gr, asked.result, checked, place, issues)


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
    seen: rules.BatchSeen | None = None,
    repair: bool = True,
    facts: dict | None = None,
    condensed: bool = False,
    max_tries: int = rules.MAX_TRIES,
) -> DescribeResult:
    """Describe one photo. At most ``max_tries`` model calls: the first answer, then repairs
    while a hard problem remains, each one a little warmer; it stops early when valid or when
    two repairs in a row bring the same answer again. What is still wrong afterwards becomes a
    warning (Needs a look)."""
    asked = _ask(
        llm,
        image_jpeg,
        style=style,
        restrictions=restrictions,
        notes=notes,
        captured=captured,
        feedback=feedback,
        max_tokens=max_tokens,
        temperature=temperature,
        retries=retries,
        timeout_s=timeout_s,
        filters=filters,
        facts=facts,
        condensed=condensed,
    )
    known_place = (facts or {}).get("place")
    location = asked.out.location.model_dump()
    detected = asked.out.detected.model_dump()

    def caption(sentence: str) -> str:
        return editorial_caption(
            location, captured, sentence, style.default_city, style.default_country, known_place
        )

    check = _Checker(style, style.limits(), seen, len(caption("")))
    answer = _assess(asked.out, check)
    calls = asked.result.retries + 1
    if repair:
        answer, calls = _repair_loop(
            llm, asked, answer, check, temperature=temperature, max_tries=max_tries
        )
    return _finish(
        llm,
        asked,
        answer,
        calls,
        filename=filename,
        style=style,
        restrictions=restrictions,
        notes=notes,
        caption=caption,
        location=location,
        detected=detected,
        known_place=known_place,
        has_model_release=has_model_release,
        has_property_release=has_property_release,
        seen=seen,
        repair=repair,
        filters=filters,
        at=at,
    )
