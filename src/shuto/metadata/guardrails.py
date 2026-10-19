"""Shutterstock guardrails: restrictions loading, prompt summary and deterministic post-check.

Pure module: explicit inputs only, no SQLite and no config access. The rules live in
``shuto/data/restrictions.yaml`` (a machine-readable copy of ``docs/shutterstock-restrictions.md``).

``detected`` is a plain dict shaped like ``llm.schema.Detected``. The check reads these optional
keys and ignores the rest: ``recognizable_people`` (or ``people``) and ``logos`` (or
``has_logos``) as bool, int or list; every other string found anywhere in the dict (landmarks,
visible text, location, ...) is matched against the rules.
"""

from __future__ import annotations

import copy
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from shuto.metadata.editorial import FORMAT_HINT, caption_problems
from shuto.metadata.rules import MIN_COMMERCIAL_WORDS

SEVERITY = {"ok": 0, "force_editorial": 1, "review": 2, "block": 3}
LEVEL_ACTIONS = {"restricted": "force_editorial", "prohibited": "block", "no_upload": "block"}
_PEOPLE_KEYS = ("recognizable_people", "people")
_LOGO_KEYS = ("logos_or_brands", "logos", "has_logos")

# What a v1 restrictions file (no detected_actions / credential_events keys) falls back to.
DEFAULT_DETECTED_ACTIONS = {
    "artworks_or_characters": "force_editorial",
    "artwork_is_main_subject": "review",
    "private_interior": "force_editorial",
    "screen_content": "force_editorial",
    "credential_event": "review",
    "id_or_card_number": "block",
    "personal_data_other": "review",
}
DEFAULT_CREDENTIAL_EVENTS = (
    "concert",
    "sports",
    "match",
    "game",
    "race",
    "rally",
    "election",
    "press conference",
    "panel",
    "red carpet",
    "runway",
    "fashion show",
    "theatre",
    "theater",
    "stage play",
    "airshow",
    "air show",
    "reenactment",
)
_ID_WORDS = frozenset({"id", "card", "passport", "licence", "license", "account", "iban"})
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# Phone-like: digit groups of 2 or more joined by space, dot, hyphen or brackets (8+ digits).
_PHONE = re.compile(r"(?<![\w])(?:\+\d{1,3}[ -]?)?(?:\(?\d{2,5}\)?[ .-]){1,4}\d{2,5}(?![\w])")
_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){11,18}\d(?!\d)")
_DATE_LIKE = re.compile(r"^\d{4}[-./]\d{1,2}[-./]\d{1,2}$|^\d{1,2}[-./]\d{1,2}[-./]\d{2,4}$")


HOUSE_RULE_PREFIX = "shuto-"


@dataclass(frozen=True)
class Rule:
    id: str
    name: str
    level: str
    category: str = "other"
    region: str = "global"
    aliases: tuple[str, ...] = ()
    condition: str | None = None
    note: str = ""

    @property
    def phrases(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)


@dataclass
class Restrictions:
    version: int
    compiled: str
    levels: dict[str, str]
    trademark_terms: list[str]
    rules: list[Rule]
    detected_actions: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_DETECTED_ACTIONS))
    credential_events: tuple[str, ...] = DEFAULT_CREDENTIAL_EVENTS
    _index: list[tuple[Rule, tuple[str, ...]]] = field(default_factory=list, repr=False)
    _trademarks: list[tuple[str, str]] = field(default_factory=list, repr=False)
    _events: list[str] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        self._index = [
            (r, tuple(normalize(p) for p in r.phrases if normalize(p))) for r in self.rules
        ]
        self._trademarks = [(t, normalize(t)) for t in self.trademark_terms if normalize(t)]
        self._events = [n for n in (normalize(e) for e in self.credential_events) if n]


@dataclass
class GuardrailHit:
    kind: str  # rule | trademark_term | people | logo | description | caption
    action: str
    where: str = ""
    rule_id: str = ""
    name: str = ""
    level: str = ""
    category: str = ""
    matched: str = ""


@dataclass
class GuardrailResult:
    action: str = "ok"  # ok | force_editorial | review | block
    hits: list[GuardrailHit] = field(default_factory=list)
    removed_keywords: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "hits": [h.__dict__ for h in self.hits],
            "removed_keywords": list(self.removed_keywords),
            "messages": list(self.messages),
        }


def normalize(text: str) -> str:
    """NFKC, casefold, accent-strip; every non-alphanumeric run becomes one space."""
    s = unicodedata.normalize("NFKC", str(text)).casefold()
    s = "".join(c for c in unicodedata.normalize("NFD", s) if not unicodedata.combining(c))
    return " ".join("".join(c if c.isalnum() else " " for c in s).split())


def _contains(haystack: str, needle: str) -> bool:
    """Whole-phrase match on normalized text."""
    return f" {needle} " in f" {haystack} "


def _parse(data: dict, source: str) -> tuple[list[Rule], list[str], dict]:
    rules: list[Rule] = []
    seen: set[str] = set()
    levels = {**LEVEL_ACTIONS, **(data.get("levels") or {})}
    for raw in data.get("rules") or []:
        try:
            rule = Rule(
                id=str(raw["id"]),
                name=str(raw["name"]),
                level=str(raw["level"]),
                category=str(raw.get("category") or "other"),
                region=str(raw.get("region") or "global"),
                aliases=tuple(str(a) for a in (raw.get("aliases") or [])),
                condition=raw.get("condition") or None,
                note=str(raw.get("note") or ""),
            )
        except KeyError as exc:
            raise ValueError(f"{source}: rule {raw!r} is missing {exc}") from exc
        if rule.level not in levels:
            raise ValueError(f"{source}: rule {rule.id} has unknown level {rule.level!r}")
        if levels[rule.level] not in SEVERITY:
            raise ValueError(f"{source}: level {rule.level} maps to unknown action")
        if rule.id in seen:
            raise ValueError(f"{source}: duplicate rule id {rule.id!r}")
        seen.add(rule.id)
        rules.append(rule)
    terms = [str(t) for t in (data.get("trademark_terms") or [])]
    return rules, terms, levels


def _read_yaml_text(text: str, source: str) -> dict:
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError(f"{source}: expected a mapping at the top level")
    return data


def load_restrictions(
    builtin: bool = True,
    extra_files: Iterable[str | Path] = (),
    disabled: Iterable[str] = (),
    house_rules: bool = True,
) -> Restrictions:
    """Load the built-in rules and merge extra files (same schema); later rules with the same id
    replace earlier ones. ``disabled`` rule ids are dropped. ``house_rules=False`` also drops
    shuto's own "shuto-*" rules (the Everyday Japan ones, which are not on Shutterstock's list)."""
    merged: dict[str, Rule] = {}
    terms: list[str] = []
    levels = dict(LEVEL_ACTIONS)
    version, compiled = 1, ""
    actions = dict(DEFAULT_DETECTED_ACTIONS)
    events: list[str] = list(DEFAULT_CREDENTIAL_EVENTS)
    sources: list[tuple[str, str]] = []
    if builtin:
        text = (
            resources.files("shuto.data").joinpath("restrictions.yaml").read_text(encoding="utf-8")
        )
        sources.append(("builtin restrictions", text))
    for f in extra_files:
        path = Path(f).expanduser()
        sources.append((str(path), path.read_text(encoding="utf-8")))
    for source, text in sources:
        data = _read_yaml_text(text, source)
        rules, file_terms, file_levels = _parse(data, source)
        levels.update(file_levels)
        for rule in rules:
            merged[rule.id] = rule
        for t in file_terms:
            if t not in terms:
                terms.append(t)
        if source == "builtin restrictions":
            version = int(data.get("version", 1))
            compiled = str(data.get("compiled", ""))
            if "credential_events" in data:
                events = [str(e) for e in data.get("credential_events") or []]
        else:
            events += [str(e) for e in data.get("credential_events") or [] if str(e) not in events]
        for signal, action in (data.get("detected_actions") or {}).items():
            if str(action) not in SEVERITY:
                raise ValueError(
                    f"{source}: detected action {signal} has unknown action {action!r}"
                )
            actions[str(signal)] = str(action)
    off = set(disabled)
    if not house_rules:
        off |= {i for i in merged if i.startswith(HOUSE_RULE_PREFIX)}
    return Restrictions(
        version=version,
        compiled=compiled,
        levels=levels,
        trademark_terms=terms,
        rules=[r for r in merged.values() if r.id not in off],
        detected_actions=actions,
        credential_events=tuple(events),
    )


def rule_action(r: Restrictions, rule: Rule) -> str:
    """Action a rule causes: a rule with a condition cannot be verified, so it goes to review."""
    if rule.condition:
        return "review"
    return r.levels[rule.level]


def prompt_summary(
    r: Restrictions, region_hint: str | None = None, max_items: int = 60, short: bool = False
) -> str:
    """Compact text for the LLM prompt: what to avoid, what makes a photo editorial.

    ``short`` is for small-context models: only the lists, and only the first 24 rules (the
    region's own come first). The general rules are already in the short prompt."""
    hint = (region_hint or "").casefold()

    def rank(rule: Rule) -> int:
        if not hint:
            return 0
        if rule.region.casefold() == hint:
            return 0
        if hint == "japan" and rule.region == "asia":
            return 1
        return 2

    ordered = sorted(r.rules, key=rank)[: max(0, min(max_items, 24) if short else max_items)]
    groups: dict[str, list[str]] = {"force_editorial": [], "block": [], "review": []}
    for rule in ordered:
        groups[rule_action(r, rule)].append(rule.name)
    lines = ["Shutterstock restrictions (non-exhaustive):"]
    if not short:
        lines += [
            "- Never put brand names, business names or trademarked terms in a commercial "
            "title or keywords (editorial captions may name them).",
            "- Visible logos, shop signs and recognizable people without releases mean editorial "
            "only.",
            f"- Commercial titles: complete English sentences, {MIN_COMMERCIAL_WORDS}+ words.",
            "- Editorial captions are built by shuto; write only caption_sentence.",
        ]
    if groups["force_editorial"]:
        lines.append(
            "Editorial only (flag editorial=true): " + "; ".join(groups["force_editorial"])
        )
    if groups["block"]:
        lines.append("Not accepted at all (flag in notes): " + "; ".join(groups["block"]))
    if groups["review"]:
        lines.append("Needs human check (flag in notes): " + "; ".join(groups["review"]))
    return "\n".join(lines)


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, (list, tuple, set)):
        for v in value:
            yield from _strings(v)


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return normalize(value) in ("yes", "true", "y", "1")
    if isinstance(value, dict):
        count = value.get("count")
        return bool(count) if count is not None else bool(value)
    return bool(value)


def _flag(detected: dict, keys: tuple[str, ...]) -> bool:
    for key in keys:
        if key in detected:
            return _truthy(detected[key])
    return False


def _bump(result: GuardrailResult, action: str) -> None:
    if SEVERITY[action] > SEVERITY[result.action]:
        result.action = action


def check(
    meta: Any,
    detected: dict | None,
    r: Restrictions,
    *,
    notes: str = "",
    has_model_release: bool = False,
    has_property_release: bool = False,
) -> tuple[Any, GuardrailResult]:
    """Deterministic post-check. Returns (adjusted copy of meta, result); never mutates ``meta``.

    Severity order: block > review > force_editorial > ok. Forced editorial only sets
    ``meta.editorial``; the caption format is validated for every photo that ends up editorial.
    """
    out = copy.deepcopy(meta)
    detected = detected or {}
    result = GuardrailResult()
    description = str(getattr(out, "description", "") or "")
    keywords = [str(k) for k in (getattr(out, "keywords", None) or [])]
    was_editorial = bool(getattr(out, "editorial", False))
    forced = False

    fields: list[tuple[str, str]] = [(f"keyword:{k}", normalize(k)) for k in keywords]
    fields.append(("description", normalize(description)))
    fields.append(("notes", normalize(notes)))
    fields.extend(("detected", normalize(s)) for s in _strings(detected))

    # Named rules.
    seen_rules: set[str] = set()
    # Batch notes cover every photo in the batch, so a rule matched only there is a hint, never
    # an action.
    rule_fields = [f for f in fields if f[0] != "notes"] + [f for f in fields if f[0] == "notes"]
    for rule, phrases in r._index:
        for where, text in rule_fields:
            hit = next((p for p in phrases if _contains(text, p)), None)
            if hit is None:
                continue
            if rule.id in seen_rules:
                break
            seen_rules.add(rule.id)
            action = rule_action(r, rule)
            if where == "notes":
                result.hits.append(
                    GuardrailHit(
                        "rule", "ok", "notes", rule.id, rule.name, rule.level, rule.category, hit
                    )
                )
                result.messages.append(
                    f"{rule.name}: mentioned in the batch notes; check the photo."
                )
                break
            if action == "force_editorial" and has_property_release and not rule.condition:
                action = "ok"
            result.hits.append(
                GuardrailHit(
                    "rule",
                    action,
                    where.split(":")[0],
                    rule.id,
                    rule.name,
                    rule.level,
                    rule.category,
                    hit,
                )
            )
            if action == "block":
                result.messages.append(
                    f"{rule.name}: not acceptable ({rule.level}). {rule.note}".strip()
                )
            elif action == "review":
                why = rule.condition or "needs a human check"
                result.messages.append(f"{rule.name}: {why}.")
            elif action == "force_editorial":
                result.messages.append(f"{rule.name}: editorial only.")
                forced = True
            _bump(result, action)
            break

    # Trademark terms: brand names are fine in an editorial photo (documentary editorial may show
    # them), so they are stripped from keywords and flagged in the description only for a
    # commercial one.
    kept: list[str] = []
    for kw, (_, text) in zip(keywords, fields[: len(keywords)], strict=True):
        term = None
        if not was_editorial:
            term = next((t for t, n in r._trademarks if _contains(text, n)), None)
        if term is None:
            kept.append(kw)
            continue
        result.removed_keywords.append(kw)
        result.hits.append(GuardrailHit("trademark_term", "ok", "keyword", name=term, matched=kw))
    if result.removed_keywords:
        result.messages.append(
            "Removed trademarked keywords: " + ", ".join(result.removed_keywords)
        )
        out.keywords = kept
    desc_text = normalize(description)
    desc_terms = [t for t, n in r._trademarks if _contains(desc_text, n)]
    for _, text in fields[len(keywords) + 2 :]:
        term = next((t for t, n in r._trademarks if _contains(text, n)), None)
        if term:
            result.hits.append(
                GuardrailHit(
                    "trademark_term", "force_editorial", "detected", name=term, matched=term
                )
            )
            result.messages.append(f"Visible brand {term!r}: editorial only.")
            _bump(result, "force_editorial")
            forced = True
            break

    # People and logos.
    if _flag(detected, _PEOPLE_KEYS) and not has_model_release:
        result.hits.append(GuardrailHit("people", "force_editorial", "detected"))
        result.messages.append("Recognizable people without a model release: editorial only.")
        _bump(result, "force_editorial")
        forced = True
    if _flag(detected, _LOGO_KEYS):
        result.hits.append(GuardrailHit("logo", "force_editorial", "detected"))
        result.messages.append("Visible logos: editorial only.")
        _bump(result, "force_editorial")
        forced = True

    forced = _detected_signals(result, detected, r, has_property_release) or forced
    if forced:
        out.editorial = True
        if not was_editorial:
            result.messages.append("Forced editorial: the description needs an editorial caption.")
    _event_notes(result, detected, r, description, bool(out.editorial))

    if not out.editorial:
        for term in desc_terms:
            result.hits.append(
                GuardrailHit("trademark_term", "review", "description", name=term, matched=term)
            )
            result.messages.append(f"Trademarked term {term!r} in the description; reword it.")
            _bump(result, "review")

    # Personal data in the text (title, description, keywords), for every photo. Kinds only.
    for kind in _text_pii_kinds([description, *keywords]):
        result.hits.append(GuardrailHit("pii", "review", "description", name=kind))
        result.messages.append("Personal data in the text: remove it.")
        _bump(result, "review")
        break

    # Metadata format checks, against the final editorial flag (a newly forced editorial photo
    # still carrying a commercial sentence goes to review).
    if out.editorial:
        problems = caption_problems(description)
        if problems:
            result.hits.append(GuardrailHit("caption", "review", "description"))
            result.messages.append(
                f"Editorial caption must look like '{FORMAT_HINT}' ({problems[0]})."
            )
            _bump(result, "review")
    elif len(description.split()) < MIN_COMMERCIAL_WORDS:
        result.hits.append(GuardrailHit("description", "review", "description"))
        result.messages.append(
            f"Commercial description needs at least {MIN_COMMERCIAL_WORDS} words."
        )
        _bump(result, "review")

    return out, result


def _text_pii_kinds(texts: Iterable[str]) -> list[str]:
    """Kinds of personal data found in the texts (never the values): email, phone, card."""
    kinds: list[str] = []
    for text in texts:
        if _EMAIL.search(text) and "email" not in kinds:
            kinds.append("email")
        if _CARD.search(text) and "card number" not in kinds:
            kinds.append("card number")
        for m in _PHONE.finditer(text):
            run = m.group()
            if sum(c.isdigit() for c in run) >= 8 and not _DATE_LIKE.match(run):
                if "phone number" not in kinds:
                    kinds.append("phone number")
    return kinds


def _kind_words(value: str) -> set[str]:
    return set(normalize(value).split())


def _is_credential_event(text: str, r: Restrictions) -> bool:
    return any(_contains(text, e) for e in r._events)


def _detected_signals(
    result: GuardrailResult, detected: dict, r: Restrictions, has_property_release: bool
) -> bool:
    """The editorial signals the model reports (artwork, interiors, screens, events, personal
    data), acted on as ``Restrictions.detected_actions`` says.

    Returns True when one of them makes the photo editorial-only."""
    forced = False

    def hit(signal: str, kind: str, message: str, makes_editorial: bool = False) -> None:
        nonlocal forced
        action = r.detected_actions.get(signal, DEFAULT_DETECTED_ACTIONS[signal])
        result.hits.append(GuardrailHit(kind, action, "detected", name=signal))
        result.messages.append(message)
        _bump(result, action)
        forced = forced or action == "force_editorial" or makes_editorial

    works = [w for w in detected.get("artworks_or_characters") or [] if str(w).strip()]
    if works and not has_property_release:
        hit("artworks_or_characters", "artwork", "Artwork, statues or characters: editorial only.")
    if _truthy(detected.get("artwork_is_main_subject")):
        hit(
            "artwork_is_main_subject",
            "artwork",
            "Artwork fills the frame: Shutterstock rejects this even as editorial.",
        )
    if _truthy(detected.get("private_interior")) and not has_property_release:
        hit(
            "private_interior",
            "interior",
            "Building interior without a property release: editorial only.",
        )
    if _truthy(detected.get("screen_content")):
        hit("screen_content", "screen", "Real content on a screen: editorial only.")
    event = str(detected.get("event") or "").strip()
    if event and _is_credential_event(normalize(event), r):
        hit(
            "credential_event",
            "event",
            f"Looks like a {event}: Shutterstock needs press credentials and a case number. "
            "Name the event in the caption.",
        )
    kinds = [str(k) for k in detected.get("personal_data") or [] if str(k).strip()]
    if any(_kind_words(k) & _ID_WORDS for k in kinds):
        hit(
            "id_or_card_number",
            "pii",
            "Readable ID, card or account number: Shutterstock does not accept it. "
            "Crop or blur it.",
        )
    if any(not (_kind_words(k) & _ID_WORDS) for k in kinds):
        hit(
            "personal_data_other",
            "pii",
            "Readable personal data (names, addresses, phone numbers): editorial only; check it.",
            makes_editorial=True,
        )
    return forced


def _event_notes(
    result: GuardrailResult, detected: dict, r: Restrictions, description: str, editorial: bool
) -> None:
    """A soft note when an editorial photo shows an event its caption does not name."""
    event = str(detected.get("event") or "").strip()
    if not event or not editorial:
        return
    text = normalize(event)
    if _is_credential_event(text, r):
        return
    said = normalize(description).split()
    if any(w in said for w in text.split() if len(w) > 3):
        return
    result.hits.append(GuardrailHit("event", "ok", "description", name=event))
    result.messages.append(f"Name the event in the caption: {event}.")
