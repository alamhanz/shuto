"""Editorial captions: build, parse, fix and check Shutterstock's documentary-editorial format.

``CITY, PREFECTURE, COUNTRY - MONTH D, YYYY: Factual sentence.`` (the prefecture or state is
left out only when it is unknown). Pure module: plain strings and dates, no place lookup, no
model call. Callers resolve the place first (``places.resolve``).

Source: https://submit.shutterstock.com/help/en/articles/10617417 (How do I submit editorial
content?) and the Contextual Metadata article. See ``docs/shutterstock-restrictions.md``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from shuto.metadata.rules import has_spaceless_script

MONTHS = (
    "JANUARY FEBRUARY MARCH APRIL MAY JUNE JULY AUGUST SEPTEMBER OCTOBER NOVEMBER DECEMBER"
).split()
PLACEHOLDERS = frozenset({"UNKNOWN", "UNIDENTIFIED", "N/A", "NONE"})
CITY_STATES = frozenset({"SINGAPORE", "MONACO", "VATICAN CITY", "VATICAN"})
MAX_CAPTION_CHARS = 2048
MIN_SENTENCE_WORDS = 3
YEAR_ONLY_BEFORE = 2000  # Shutterstock: before 2000 the year (or decade) is enough

FORMAT_HINT = "CITY, PREFECTURE, COUNTRY - MONTH D, YYYY: Sentence."

_MONTH_NUMBER = {name: i for i, name in enumerate(MONTHS, 1)}
_MONTH_NUMBER.update({name[:3]: i for i, name in enumerate(MONTHS, 1)})
_MONTH_NUMBER["SEPT"] = 9

_PLACE_PART = r"[^\W\d_](?:[^\W\d_]|[ .'/-])*"
_PLACE = re.compile(rf"^(?P<parts>{_PLACE_PART}(?:, {_PLACE_PART}){{1,2}})$")
_DATE = re.compile(r"^(?:(?P<month>[^\W\d_]+) (?P<day>\d{1,2}), (?P<year>\d{4})|(?P<only>\d{4}))$")
_CAPTION = re.compile(r"^(?P<place>[^:]+?) - (?P<date>[^:]+?): (?P<sentence>\S.*)$", re.DOTALL)
# Lenient reading of a hand-edited caption, for fix_caption().
_ORD = r"(?:st|nd|rd|th)?"
_LOOSE = re.compile(
    r"^(?P<place>[^:]+?)\s*(?:\s[-–—]\s|[–—])\s*"
    rf"(?:(?P<m1>[A-Za-z]{{3,9}})\.?\s+(?P<d1>\d{{1,2}}){_ORD},?\s+(?P<y1>\d{{4}})"
    rf"|(?P<d2>\d{{1,2}}){_ORD}\s+(?P<m2>[A-Za-z]{{3,9}})\.?,?\s+(?P<y2>\d{{4}})"
    r"|(?P<y3>\d{4}))"
    r"\s*(?::|\s[-–—]\s|[–—])\s*(?P<sentence>\S.*)$",
    re.DOTALL,
)
_LEADING_DATE = re.compile(
    r"^(?:[A-Za-z]{3,9}\.? \d{1,2}(?:st|nd|rd|th)?,? \d{4}|\d{4}-\d{2}-\d{2})\b"
)
_LEADING_HEAD = re.compile(r"^[A-Z][A-Z .'/-]+(?:, [A-Z][A-Z .'/-]+){1,2} [-–—] ")


@dataclass(frozen=True)
class CaptionParts:
    city: str
    region: str
    country: str
    date: date | None
    year_only: int | None
    sentence: str


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def capitalise(text: str) -> str:
    text = _squash(text)
    return text[:1].upper() + text[1:]


def _place_text(text: str) -> str:
    return _squash(text).upper()


def build_caption(
    city: str, country: str, taken: date | None, sentence: str, region: str = ""
) -> str:
    """`CITY, PREFECTURE, COUNTRY - MONTH D, YYYY: Sentence`.

    A missing city, country or date becomes UNKNOWN (the caption then fails the check, so it is
    never exported by accident). A missing prefecture is simply left out.
    """
    parts = [_place_text(city) or "UNKNOWN"]
    if _place_text(region):
        parts.append(_place_text(region))
    parts.append(_place_text(country) or "UNKNOWN")
    date_s = f"{MONTHS[taken.month - 1]} {taken.day}, {taken.year}" if taken else "UNKNOWN"
    return f"{', '.join(parts)} - {date_s}: {capitalise(sentence)}"


def _parse_date(text: str) -> tuple[date | None, int | None] | None:
    m = _DATE.match(text)
    if not m:
        return None
    if m.group("only"):
        return None, int(m.group("only"))
    month = _MONTH_NUMBER.get(m.group("month"))
    if m.group("month") not in MONTHS or month is None:  # English, upper case, full name
        return None
    try:
        return date(int(m.group("year")), month, int(m.group("day"))), None
    except ValueError:
        return None


def parse_caption(text: str) -> CaptionParts | None:
    """The parts of a well-formed caption, else None.

    Well-formed: upper-case place of two or three comma-separated names, a real
    `MONTH D, YYYY` date (or only a year) and a sentence. UNKNOWN places parse (the checks
    report them); an UNKNOWN date does not.
    """
    m = _CAPTION.match(_squash(text))
    if not m:
        return None
    place = _PLACE.match(m.group("place"))
    if not place or m.group("place") != m.group("place").upper():
        return None
    names = place.group("parts").split(", ")
    parsed = _parse_date(m.group("date"))
    if parsed is None:
        return None
    region = names[1] if len(names) == 3 else ""
    return CaptionParts(names[0], region, names[-1], parsed[0], parsed[1], m.group("sentence"))


def _has_placeholder(parts: CaptionParts) -> bool:
    return any(p in PLACEHOLDERS for p in (parts.city, parts.region, parts.country))


def is_editorial_caption(text: str) -> bool:
    """True for a well-formed caption with no UNKNOWN or UNIDENTIFIED place part."""
    parts = parse_caption(text)
    return parts is not None and not _has_placeholder(parts)


def fix_caption(text: str) -> str:
    """Deterministic repairs of a hand-edited caption; anything else is returned unchanged.

    Dash variants become " - ", the place and month are upper-cased, "July 31 2026",
    "July 31st 2026" and "31 July 2026" become "JULY 31, 2026", a missing space after ":" is
    added and the sentence starts with a capital letter. Never invents a part.
    """
    text = _squash(text)
    m = _LOOSE.match(text)
    if not m:
        return text
    if m.group("y3"):
        date_s = m.group("y3")
    else:
        month = (m.group("m1") or m.group("m2") or "").upper()
        number = _MONTH_NUMBER.get(month)
        if number is None:
            return text
        day = m.group("d1") or m.group("d2")
        year = m.group("y1") or m.group("y2")
        date_s = f"{MONTHS[number - 1]} {int(day)}, {year}"
    place = ", ".join(_place_text(p) for p in m.group("place").split(","))
    return f"{place} - {date_s}: {capitalise(m.group('sentence'))}"


def sentence_problems(sentence: str, min_words: int = MIN_SENTENCE_WORDS) -> list[str]:
    """Problems of the factual sentence alone (what the model writes)."""
    text = _squash(sentence)
    out: list[str] = []
    if len(text.split()) < min_words and not has_spaceless_script(text):
        out.append(f"the sentence needs at least {min_words} words")
    if text and text[0].islower():
        out.append("the sentence must start with a capital letter")
    if _LEADING_DATE.match(text) or _LEADING_HEAD.match(text):
        out.append("the sentence repeats the date or place; they are added in front")
    return out


def _diagnose(text: str) -> str:
    """The most useful reason a caption does not parse."""
    m = _CAPTION.match(_squash(text))
    if not m:
        return "the place and date in front of the sentence are missing or in the wrong form"
    place, when = m.group("place"), m.group("date")
    if when.strip().upper() in PLACEHOLDERS:
        return "the date is missing (UNKNOWN)"
    if place != place.upper():
        return "the place must be in capital letters"
    if not _PLACE.match(place):
        return "the place must be CITY, PREFECTURE, COUNTRY (letters only)"
    loose = re.match(r"^(?P<month>[^\W\d_]+) (?P<day>\d{1,2}), (?P<year>\d{4})$", when)
    if loose:
        if loose.group("month") not in MONTHS:
            return "the month must be an English month name in capital letters"
        return "the date is not a real calendar date"
    return "the date must look like MONTH D, YYYY (for example JULY 31, 2026)"


def caption_problems(text: str, taken: date | None = None, today: date | None = None) -> list[str]:
    """What is wrong with an editorial caption; an empty list means it can be exported.

    ``taken`` is the photo's capture date when known: the caption must show the same day.
    """
    text = _squash(text)
    parts = parse_caption(text)
    if parts is None:
        return [_diagnose(text)]
    out: list[str] = []
    if len(text) > MAX_CAPTION_CHARS:
        out.append(f"it is over {MAX_CAPTION_CHARS} characters")
    named = {"city": parts.city, "prefecture": parts.region, "country": parts.country}
    for label, value in named.items():
        if value in PLACEHOLDERS:
            out.append(f"the {label} is unknown ({value}); set a default place or edit it")
    if parts.city == parts.country and parts.city not in CITY_STATES:
        out.append("the city repeats the country")
    when = parts.date
    if when is not None:
        if when > (today or date.today()):
            out.append("the date is in the future")
        if taken is not None and when != taken:
            out.append("the date differs from the photo's capture date")
    elif parts.year_only is not None:
        if parts.year_only >= YEAR_ONLY_BEFORE:
            out.append(f"a year alone is only allowed before {YEAR_ONLY_BEFORE}; add month and day")
        elif taken is not None and parts.year_only != taken.year:
            out.append("the year differs from the photo's capture date")
    out += sentence_problems(parts.sentence)
    return out


_SHAPE = re.compile(
    r"^(?P<place>[^\W\d_a-z][^a-z:]*?) [-–—] (?P<date>[^:]{1,24}): (?P<sentence>\S.*)$", re.DOTALL
)


def caption_sentence_of(text: str) -> str | None:
    """The sentence of a text that has a caption's shape (upper-case place, date, colon), even
    when its date or place is UNKNOWN or badly written; None for any other text."""
    squashed = _squash(fix_caption(text))
    m = _SHAPE.match(squashed)
    return m.group("sentence") if m else None
