"""Value cleaners shared by the config and the saved settings: place names, language, tries."""

from __future__ import annotations

import re
import unicodedata

MAX_TRIES = 8  # model calls per photo: the first answer plus repairs
MAX_LANGUAGE_CHARS = 30


MAX_PLACE_CHARS = 60
PLACE_NAME_RE = re.compile(r"[A-Za-z][A-Za-z .'-]*")
_PLACE_OK = PLACE_NAME_RE  # alias until T9


def clean_place_part(text: object) -> str:
    """A city or country typed by the user: accents dropped, spaces tidied, letters only.

    Returns "" for empty input; raises ValueError (plain words) for anything else unusable.
    """
    raw = unicodedata.normalize("NFKD", str(text or ""))
    value = re.sub(r"\s+", " ", "".join(c for c in raw if not unicodedata.combining(c))).strip()
    if not value:
        return ""
    if len(value) > MAX_PLACE_CHARS:
        raise ValueError(f"Keep each place name under {MAX_PLACE_CHARS} characters.")
    if not PLACE_NAME_RE.fullmatch(value):
        raise ValueError("Write the place in English letters, for example Tokyo or Japan.")
    return value


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
