"""Deterministic checks and fixes for the description and keywords (the Shutterstock guide).

Pure module: no LLM, no database, no config files. ``describe`` applies the fixes, asks the
model to repair its answer (up to ``MAX_TRIES`` model calls per photo) while a hard rule still
fails, and reports what is left.

Hard problems (a repair is worth a slow model call) are listed in ``HARD``. Everything else is
only a note: too few keywords for the 25 to 45 sweet spot, a short or long description, a
repeated word.
"""

from __future__ import annotations

import re
import threading
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from itertools import islice

MIN_WORDS = 5
KEYWORD_FLOOR = 7  # Shutterstock's minimum; below it the photo cannot be sent
DESC_JACCARD = 0.9
KEYWORD_JACCARD = 0.85
MAX_KEYWORD_WORDS = 6
MAX_KEYWORD_CHARS = 50
MAX_TRIES = 8  # model calls per photo: the first answer plus repairs

HARD = frozenset(
    {
        "too_few_words",
        "too_long",
        "comma_list",
        "few_keywords",
        "dup_description",
        "dup_keywords",
        "non_english",
    }
)

_STOP = frozenset(
    "a an the of in on at to with and or for from by as is are was were be been it its this that "
    "these those into over under near next up down out off than then there their his her they "
    "while during against between through about after before above below".split()
)
_ALLOWED_PUNCT = frozenset(".,'\"-:;!?()&/%")
_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"'})
_DASHES = str.maketrans({"–": "-", "—": "-", "−": "-"})
_WORD = re.compile(r"[^\W_]+(?:['-][^\W_]+)*")
_CAPTION_PREFIX = re.compile(r"^[^:]{1,120} - [A-Za-z]+ \d{1,2}, \d{4}: ")
_UNKNOWN_PREFIX = re.compile(r"^UNKNOWN, UNKNOWN - UNKNOWN: ")


@dataclass(frozen=True)
class Limits:
    """The numbers the owner can tune (config ``style.description`` and ``style.keywords``)."""

    desc_target_min: int = 70
    desc_target_max: int = 150
    desc_max: int = 200
    kw_min: int = 25
    kw_max: int = 45


DEFAULT_LIMITS = Limits()


@dataclass(frozen=True)
class Issue:
    code: str
    text: str

    @property
    def hard(self) -> bool:
        return self.code in HARD

    def to_dict(self) -> dict:
        return {"code": self.code, "text": self.text, "hard": self.hard}


def reason(code: str, limits: Limits = DEFAULT_LIMITS, count: int = 0) -> str:
    """The plain-words reason for a problem code."""
    return {
        "too_few_words": f"Description has fewer than {MIN_WORDS} words",
        "too_long": f"Description is over {limits.desc_max} characters",
        "comma_list": "Description reads like a list of words",
        "few_keywords": f"Only {count} keywords (Shutterstock needs at least {KEYWORD_FLOOR})",
        "light_keywords": f"Only {count} keywords (aim for {limits.kw_min} to {limits.kw_max})",
        "dup_description": "Description is almost the same as another photo in this batch",
        "dup_keywords": "Keywords are almost the same as another photo in this batch",
        "non_english": "Text is not in English (only English, Latin letters are allowed)",
        "repeated_words": "Description repeats a word",
        "short_description": (
            f"Description is short (aim for {limits.desc_target_min} to "
            f"{limits.desc_target_max} characters)"
        ),
        "long_description": (
            f"Description is long (aim for {limits.desc_target_min} to "
            f"{limits.desc_target_max} characters)"
        ),
    }.get(code, code.replace("_", " ").capitalize())


# ------------------------------------------------------------------------------ English only

_LATIN_EXTRA = str.maketrans(
    {"ß": "ss", "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE", "ø": "o", "Ø": "O", "đ": "d", "ł": "l"}
)


def ascii_fold(text: str | None) -> str:
    """Latin letters with accents become plain ASCII (café -> cafe, Kyōto -> Kyoto, ß -> ss).

    Only Latin letters are touched. Other scripts (kana, kanji, Cyrillic, ...) are returned
    exactly as they came, so they can be detected and reported rather than silently altered.
    """
    out: list[str] = []
    for ch in unicodedata.normalize("NFC", str(text or "")):
        if ch.isascii():
            out.append(ch)
            continue
        if not unicodedata.name(ch, "").startswith(("LATIN", "FULLWIDTH LATIN")):
            out.append(ch)
            continue
        plain = "".join(
            c for c in unicodedata.normalize("NFKD", ch.translate(_LATIN_EXTRA)) if not c.isspace()
        )
        plain = "".join(c for c in plain if not unicodedata.combining(c))
        out.append(plain if plain.isascii() else ch)
    return "".join(out)


def has_foreign_script(text: str | None) -> bool:
    """True when the text has letters or digits that are not plain ASCII after ``ascii_fold``.

    Assumption: Shutterstock text is English written with ASCII letters; normal accents are
    folded to ASCII first, anything else (Japanese, Chinese, Korean, Cyrillic, Arabic, ...)
    counts as foreign.
    """
    return any(not c.isascii() and (c.isalpha() or c.isdecimal()) for c in ascii_fold(text))


def english_only(text: str | None) -> str:
    """The text with accents folded and every word that has non-Latin letters left out."""
    words = [w for w in ascii_fold(text).split() if not has_foreign_script(w)]
    return re.sub(r"\s+", " ", " ".join(words)).strip()


MAX_LANGUAGE_CHARS = 30
ENGLISH = "English"


def clean_language(value: object) -> str:
    """A language name for the prompt: letters, spaces and hyphens only (max 30 characters).

    The name goes into the model's instructions, so anything else (digits, quotes, line breaks,
    punctuation) is refused. Raises ValueError with a plain-words message.
    """
    text = " ".join(str(value or "").split())
    if not text:
        raise ValueError("Type the language to write in, for example Japanese.")
    if len(text) > MAX_LANGUAGE_CHARS:
        raise ValueError(f"Keep the language name under {MAX_LANGUAGE_CHARS} characters.")
    if not text[0].isalpha() or any(not (c.isalpha() or c in " -") for c in text):
        raise ValueError("Use only letters, spaces and hyphens in the language name.")
    return text


def check_english(description: str, keywords: Sequence[str]) -> list[Issue]:
    if has_foreign_script(description) or any(has_foreign_script(k) for k in keywords):
        return [Issue("non_english", reason("non_english"))]
    return []


# ----------------------------------------------------------------------------- text fixes


def tidy_text(text: str | None) -> tuple[str, bool]:
    """Description text without emoji or special characters, one line. Returns (text, changed).

    Letters (any language) and digits stay, plus ordinary punctuation. Curly quotes and long
    dashes become plain ones; a changed flag is set only when something was removed.
    """
    raw = unicodedata.normalize("NFC", str(text or "")).translate(_QUOTES).translate(_DASHES)
    kept = "".join(c if (c.isalnum() or c.isspace() or c in _ALLOWED_PUNCT) else " " for c in raw)
    clean = re.sub(r"\s+", " ", kept).strip()
    dropped = re.sub(r"\s+", "", raw) != re.sub(r"\s+", "", kept)
    return clean, dropped


def words_of(text: str) -> list[str]:
    return _WORD.findall(text or "")


def singular(word: str) -> str:
    """A simple English singular: ies -> y, es after s/x/z/ch/sh, else drop a final s."""
    w = word.lower()
    if len(w) <= 3:
        return w
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if w.endswith(("sses", "shes", "ches", "xes", "zes")):
        return w[:-2]
    if w.endswith(("ss", "us", "is")):
        return w
    if w.endswith("s"):
        return w[:-1]
    return w


def _function_ratio(words: Sequence[str]) -> float:
    return sum(1 for w in words if w.lower() in _STOP) / len(words) if words else 0.0


def is_comma_list(text: str) -> bool:
    """True when the text is mostly short comma-separated chunks (a keyword list)."""
    chunks = [c for c in re.split(r"[,;|]", text or "") if c.strip()]
    if len(chunks) < 3:
        return False
    words = words_of(text)
    if not words:
        return False
    if len(words) / len(chunks) <= 2.5:
        return True
    return len(chunks) >= 4 and _function_ratio(words) < 0.12


def repeated_words(text: str) -> list[str]:
    """Meaningful words that appear more than once (stop words and numbers ignored)."""
    seen: dict[str, int] = {}
    for w in words_of(text):
        low = w.lower()
        if low in _STOP or low.isdigit() or len(low) < 3:
            continue
        key = singular(low)
        seen[key] = seen.get(key, 0) + 1
    return [w for w, n in seen.items() if n > 1]


def strip_caption_prefix(text: str) -> str:
    """The sentence part of an editorial caption ('CITY, COUNTRY - MONTH D, YYYY: text')."""
    return _CAPTION_PREFIX.sub("", _UNKNOWN_PREFIX.sub("", text or ""), count=1)


_SPACELESS = ("CJK", "HIRAGANA", "KATAKANA", "THAI", "LAO", "KHMER", "MYANMAR")


def has_spaceless_script(text: str | None) -> bool:
    """True when the text has letters of a script written without spaces (Japanese, Thai, ...)."""
    return any(
        unicodedata.name(c, "").startswith(_SPACELESS) for c in text or "" if not c.isascii()
    )


def check_description(
    sentence: str, limits: Limits = DEFAULT_LIMITS, prefix_len: int = 0, english: bool = True
) -> list[Issue]:
    """Problems of the sentence. ``prefix_len`` is the length of an editorial prefix: the total
    must stay within the hard maximum, while the target range applies to the sentence alone.

    ``english=False`` (another metadata language) skips the English word-list checks and the
    word count of scripts that are written without spaces."""
    text = (sentence or "").strip()
    out: list[Issue] = []

    def add(code: str) -> None:
        out.append(Issue(code, reason(code, limits)))

    n_words = len(words_of(text))
    if not english and has_spaceless_script(text):
        n_words = max(n_words, MIN_WORDS)
    if n_words < MIN_WORDS:
        add("too_few_words")
    if prefix_len + len(text) > limits.desc_max:
        add("too_long")
    if english and is_comma_list(text):
        add("comma_list")
    if english and repeated_words(text):
        add("repeated_words")
    if n_words >= MIN_WORDS:
        if len(text) < limits.desc_target_min:
            add("short_description")
        elif len(text) > limits.desc_target_max and "too_long" not in {i.code for i in out}:
            add("long_description")
    return out


# ------------------------------------------------------------------------------- keywords


def _keyword_key(words: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted(singular(w) for w in words))


def _clean_keyword(raw: object) -> str:
    text = re.sub(r"[^\w\s'-]|_", " ", str(raw or "").lower())
    text = re.sub(r"\s+", " ", text).strip(" -'")
    return text


def clean_keywords(
    raw: Iterable[object],
    limits: Limits = DEFAULT_LIMITS,
    avoid: Iterable[str] = (),
    always_include: Iterable[str] = (),
) -> list[str]:
    """Lower-case, trimmed, de-duplicated keywords in their original (relevance) order.

    Drops empty ones, overlong phrases, repeats (also singular/plural twins and the same words
    in another order) and keywords containing an avoided word. Caps at ``limits.kw_max``.
    The "always include" terms are kept: any the model already wrote stay where the model put
    them; the missing ones are added at the end (they are portfolio tags, not the most
    relevant words for this photo) and are never cut by the cap.
    """
    avoid_keys = {singular(a.strip().lower()) for a in avoid if a and a.strip()}
    out: list[str] = []
    seen: set[tuple[str, ...]] = set()
    for item in raw or []:
        kw = _clean_keyword(item)
        ws = words_of(kw)
        if (
            not ws
            or len(ws) > MAX_KEYWORD_WORDS
            or len(kw) > MAX_KEYWORD_CHARS
            or (len(kw) < 2 and kw.isascii())
        ):
            continue
        key = _keyword_key(ws)
        if key in seen or (avoid_keys and avoid_keys & {singular(w) for w in ws}):
            continue
        seen.add(key)
        out.append(kw)
    extra: list[str] = []
    wanted: set[tuple[str, ...]] = set()
    for item in always_include:
        kw = _clean_keyword(item)
        ws = words_of(kw)
        if not ws:
            continue
        key = _keyword_key(ws)
        wanted.add(key)
        if key not in seen:
            seen.add(key)
            extra.append(kw)
    cap = max(1, limits.kw_max)
    if len(out) + len(extra) > cap:
        room = max(0, cap - len(extra))
        kept_wanted = [k for k in out if _keyword_key(words_of(k)) in wanted]
        room_for_rest = max(0, room - len(kept_wanted))
        rest = iter(k for k in out if k not in kept_wanted)
        allowed = set(kept_wanted) | set(islice(rest, room_for_rest))
        out = [k for k in out if k in allowed]
    return out + extra


def check_keywords(keywords: Sequence[str], limits: Limits = DEFAULT_LIMITS) -> list[Issue]:
    n = len(keywords)
    if n < KEYWORD_FLOOR:
        return [Issue("few_keywords", reason("few_keywords", limits, n))]
    if n < limits.kw_min:
        return [Issue("light_keywords", reason("light_keywords", limits, n))]
    return []


# ----------------------------------------------------------------------------- uniqueness


def _content(text: str) -> frozenset[str]:
    lows = (w.lower() for w in words_of(strip_caption_prefix(text)))
    return frozenset(singular(w) for w in lows if w not in _STOP)


def _jaccard(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


class BatchSeen:
    """Descriptions and keyword sets already written in this batch (safe across threads)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: list[tuple[str, frozenset[str], frozenset[tuple[str, ...]]]] = []

    def __len__(self) -> int:
        return len(self._items)

    @staticmethod
    def _parts(description: str, keywords: Iterable[str]):
        words = _content(description)
        keys = frozenset(_keyword_key(words_of(k)) for k in keywords if words_of(k))
        return strip_caption_prefix(description), words, keys

    def add(self, description: str, keywords: Iterable[str]) -> None:
        text, words, keys = self._parts(description, keywords)
        with self._lock:
            self._items.append((text, words, keys))

    def find(self, description: str, keywords: Iterable[str]) -> dict[str, str | None]:
        """{"description": the near-identical earlier description or None, "keywords": the
        earlier description whose keyword set is near-identical or None}."""
        _, words, keys = self._parts(description, keywords)
        hit: dict[str, str | None] = {"description": None, "keywords": None}
        with self._lock:
            items = list(self._items)
        for text, w, k in items:
            if hit["description"] is None and _jaccard(words, w) >= DESC_JACCARD:
                hit["description"] = text
            if hit["keywords"] is None and _jaccard(keys, k) >= KEYWORD_JACCARD:
                hit["keywords"] = text
        return hit


def check_unique(seen: BatchSeen | None, description: str, keywords: Sequence[str]) -> list[Issue]:
    if seen is None or not len(seen):
        return []
    hit = seen.find(description, keywords)
    out = []
    if hit["description"] is not None:
        out.append(Issue("dup_description", reason("dup_description")))
    if hit["keywords"] is not None:
        out.append(Issue("dup_keywords", reason("dup_keywords")))
    return out


def similar_to(seen: BatchSeen | None, description: str, keywords: Sequence[str]) -> list[str]:
    """The earlier descriptions this one is too close to (for the repair prompt)."""
    if seen is None:
        return []
    hit = seen.find(description, keywords)
    return list(dict.fromkeys(t for t in hit.values() if t))
