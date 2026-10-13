"""Photo filters answered inside the one describe call: theme fit and the user's own rules.

Pure module: no SQLite, no config files, no network. Everything the model says about the
filters is read leniently and never raises. A missing, garbled or unusable answer becomes
"unknown", which the pipeline shows as "Needs a look" (never a silent pass).
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import unicodedata
from dataclasses import dataclass, field
from typing import Any

MAX_RULES = 10
MIN_RULE_TEXT = 2
MAX_RULE_TEXT = 80
MAX_REASON_CHARS = 160
KINDS = ("include", "avoid")
MODES = ("set_aside", "flag")
PARTLY_MODES = ("pass", "flag")
UNKNOWN = "unknown"

_ID_RE = re.compile(r"^[a-z0-9]{1,12}$")
_YES = {"yes", "y", "true", "1", "yep", "yeah", "correct"}
_NO = {"no", "n", "false", "0", "nope", "none"}
_PARTLY = {"partly", "partial", "partially", "somewhat", "kind of", "sort of"}
_EDGE = " \t\r\n\"'`.,;:!?()[]{}*"


class FilterRuleError(ValueError):
    """A rule the user typed is not usable. The message is plain English, shown in the UI."""


@dataclass(frozen=True)
class UserRule:
    id: str
    kind: str
    text: str
    mode: str = "set_aside"

    def label(self) -> str:
        return f"{'Must include' if self.kind == 'include' else 'Must avoid'}: {self.text}"


def _new_id() -> str:
    return "r" + secrets.token_hex(4)


def _clean_text(value: Any) -> str:
    text = unicodedata.normalize("NFC", "" if value is None else str(value))
    if any(unicodedata.category(c) == "Cc" and c not in "\t\r\n" for c in text):
        raise FilterRuleError("A rule can't contain hidden control characters.")
    return re.sub(r"\s+", " ", text).strip()


def _field(row: Any, name: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(name, default)
    return getattr(row, name, default)


def clean_rules(value: Any) -> list[UserRule]:
    """Validate and normalize rules from config/settings (dicts or UserRule objects).

    Drops rows with empty text, removes duplicates (same kind and text ignoring case), keeps
    ids when valid and unique, and raises FilterRuleError for anything unusable.
    """
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise FilterRuleError("Rules must be a list.")
    out: list[UserRule] = []
    seen: set[tuple[str, str]] = set()
    ids: set[str] = set()
    for row in value:
        text = _clean_text(_field(row, "text"))
        if not text:
            continue
        kind = str(_field(row, "kind", "") or "").strip().lower()
        if kind not in KINDS:
            raise FilterRuleError(f'Rule "{text}": choose "Must include" or "Must avoid".')
        mode = str(_field(row, "mode", "set_aside") or "set_aside").strip().lower()
        if mode not in MODES:
            raise FilterRuleError(f'Rule "{text}": choose "Set aside" or "Just flag".')
        if len(text) < MIN_RULE_TEXT:
            raise FilterRuleError(f"A rule needs at least {MIN_RULE_TEXT} characters.")
        if len(text) > MAX_RULE_TEXT:
            raise FilterRuleError(
                f"A rule can be at most {MAX_RULE_TEXT} characters; "
                f'"{text[:20]}..." is {len(text)}.'
            )
        key = (kind, text.casefold())
        if key in seen:
            continue
        seen.add(key)
        rid = str(_field(row, "id", "") or "").strip()
        while not _ID_RE.match(rid) or rid in ids:
            rid = _new_id()
        ids.add(rid)
        out.append(UserRule(id=rid, kind=kind, text=text, mode=mode))
    if len(out) > MAX_RULES:
        raise FilterRuleError(f"You can have at most {MAX_RULES} rules.")
    return out


@dataclass(frozen=True)
class FilterSpec:
    theme: str = ""
    theme_check: bool = True
    theme_partly: str = "pass"  # pass | flag
    rules: tuple[UserRule, ...] = field(default_factory=tuple)

    @property
    def theme_active(self) -> bool:
        return bool(self.theme_check and self.theme.strip())

    @property
    def active(self) -> bool:
        return self.theme_active or bool(self.rules)

    def fingerprint(self) -> str:
        """Short stable hash of what a photo was checked against (theme if active, rules)."""
        canon = {
            "theme": self.theme.strip() if self.theme_active else "",
            "partly": self.theme_partly if self.theme_active else "",
            "rules": [[r.kind, r.text, r.mode] for r in self.rules],
        }
        blob = json.dumps(canon, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


def _quote(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)


def prompt_lines(spec: FilterSpec) -> list[str]:
    """Prompt block for the describe call; empty when no filter is active."""
    if not spec.active:
        return []
    lines = ["Photo checks (answer every one; keep each reason under 20 words):"]
    if spec.theme_active:
        lines.append(
            f'- "theme_fit": does this photo fit the portfolio theme {_quote(spec.theme.strip())}? '
            'Give "reason", then "fit": "yes", "partly" or "no".'
        )
    if spec.rules:
        lines += [
            '- "checks": an object with one entry per statement below. For each, look only at '
            'the photo and give "reason", then "answer": "yes" if the statement is in or true '
            'of the photo, else "no".'
        ]
        lines += [f"  {_quote(f'r{i}')}: {_quote(r.text)}" for i, r in enumerate(spec.rules, 1)]
    return lines


def _answer_object(key: str, values: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {"reason": {"type": "string"}, key: {"type": "string", "enum": values}},
        "required": ["reason", key],
    }


def schema_properties(spec: FilterSpec) -> tuple[dict, list[str]]:
    """Extra top-level schema properties and required names. Inline, no `$ref`."""
    props: dict = {}
    required: list[str] = []
    if spec.theme_active:
        props["theme_fit"] = _answer_object("fit", ["yes", "partly", "no"])
        required.append("theme_fit")
    if spec.rules:
        keys = [f"r{i}" for i in range(1, len(spec.rules) + 1)]
        props["checks"] = {
            "type": "object",
            "properties": {k: _answer_object("answer", ["yes", "no"]) for k in keys},
            "required": keys,
        }
        required.append("checks")
    return props, required


def normalize_answer(v: Any, allowed: tuple[str, ...]) -> str:
    """Map a loose model answer to one of `allowed`, else "unknown". Never raises."""
    if isinstance(v, bool):
        v = "yes" if v else "no"
    elif isinstance(v, int) and v in (0, 1):
        v = "yes" if v else "no"
    if not isinstance(v, str):
        return UNKNOWN
    s = re.sub(r"\s+", " ", v.strip(_EDGE).lower())
    for cand in (s, re.split(r"[\s,.;:!?-]+", s, maxsplit=1)[0] if s else ""):
        if cand in allowed:
            return cand
        for word, vocab in (("yes", _YES), ("no", _NO), ("partly", _PARTLY)):
            if cand in vocab and word in allowed:
                return word
    return UNKNOWN


def _reason(v: Any) -> str:
    if not isinstance(v, str):
        return ""
    text = re.sub(r"\s+", " ", v).strip()
    if len(text) > MAX_REASON_CHARS:
        text = text[: MAX_REASON_CHARS - 3].rstrip() + "..."
    return text


def _split_entry(entry: Any, verdict_keys: tuple[str, ...]) -> tuple[Any, str]:
    """Return (raw answer, reason) from a dict entry or a bare answer value."""
    if isinstance(entry, dict):
        raw: Any = None
        for k in verdict_keys:
            if k in entry:
                raw = entry[k]
                break
        return raw, _reason(entry.get("reason"))
    return entry, ""


def _find_check(checks_raw: Any, index: int, rule: UserRule) -> Any:
    """Locate the entry for rule number `index` (1-based) in a dict or list answer."""
    names = (f"r{index}", rule.id, rule.text)
    if isinstance(checks_raw, dict):
        for n in names:
            if n in checks_raw:
                return checks_raw[n]
        lowered = {str(k).strip().lower(): v for k, v in checks_raw.items()}
        for n in names:
            if n.lower() in lowered:
                return lowered[n.lower()]
        return None
    if isinstance(checks_raw, list):
        unlabeled: list[Any] = []
        for item in checks_raw:
            label = None
            if isinstance(item, dict):
                label = item.get("id", item.get("rule"))
            if label is None:
                unlabeled.append(item)
            elif str(label).strip().lower() in {n.lower() for n in names}:
                return item
        if len(unlabeled) == len(checks_raw) and index <= len(unlabeled):
            return unlabeled[index - 1]  # no labels at all: trust the order
    return None


def _verdict(effects: list[str]) -> str:
    if "set_aside" in effects:
        return "set_aside"
    return "flag" if "flag" in effects else "ok"


@dataclass
class FilterResult:
    at: str
    model: str
    fingerprint: str
    theme: dict | None = None
    rules: list[dict] = field(default_factory=list)
    overridden: dict | None = None

    @property
    def effects(self) -> list[str]:
        items = ([self.theme] if self.theme else []) + self.rules
        return [i["effect"] for i in items]

    @property
    def verdict(self) -> str:
        return _verdict(self.effects)

    @property
    def set_aside_by(self) -> list[str]:
        by = ["theme"] if self.theme and self.theme["effect"] == "set_aside" else []
        return by + [f"rule:{r['id']}" for r in self.rules if r["effect"] == "set_aside"]

    def to_dict(self) -> dict:
        return {
            "v": 1,
            "at": self.at,
            "model": self.model,
            "fingerprint": self.fingerprint,
            "verdict": self.verdict,
            "theme": self.theme,
            "rules": self.rules,
            "set_aside_by": self.set_aside_by,
            "overridden": self.overridden,
        }


def _theme_effect(fit: str, spec: FilterSpec) -> str:
    if fit == "no":
        return "set_aside"
    if fit == "partly":
        return "flag" if spec.theme_partly == "flag" else "ok"
    return "ok" if fit == "yes" else "flag"


def _rule_result(rule: UserRule, answer: str) -> tuple[str, str]:
    """Return (result, effect): the statement is neutral, kind decides what yes means."""
    if answer == UNKNOWN:
        return UNKNOWN, "flag"
    met = (answer == "yes") == (rule.kind == "include")
    return ("pass", "ok") if met else ("fail", rule.mode)


def evaluate(
    spec: FilterSpec, theme_raw: Any, checks_raw: Any, *, model: str, at: str
) -> FilterResult:
    """Turn the model's raw filter answers into a FilterResult. Never raises."""
    res = FilterResult(at=at, model=model, fingerprint=spec.fingerprint())
    if spec.theme_active:
        raw, reason = _split_entry(theme_raw, ("fit", "answer", "verdict"))
        fit = normalize_answer(raw, ("yes", "partly", "no"))
        res.theme = {
            "text": spec.theme.strip(),
            "fit": fit,
            "reason": reason,
            "effect": _theme_effect(fit, spec),
        }
    for i, rule in enumerate(spec.rules, 1):
        raw, reason = _split_entry(_find_check(checks_raw, i, rule), ("answer", "fit", "verdict"))
        answer = normalize_answer(raw, ("yes", "no"))
        result, effect = _rule_result(rule, answer)
        res.rules.append(
            {
                "id": rule.id,
                "kind": rule.kind,
                "text": rule.text,
                "mode": rule.mode,
                "answer": answer,
                "result": result,
                "reason": reason,
                "effect": effect,
            }
        )
    return res


def status_effect(filters: dict | None) -> str:
    """ "ok", "flag" or "set_aside" for a stored filters dict (None = not checked = ok)."""
    if not isinstance(filters, dict) or filters.get("overridden"):
        return "ok"
    items = [filters.get("theme")] + list(filters.get("rules") or [])
    return _verdict([i.get("effect") for i in items if isinstance(i, dict)])


def _with_reason(head: str, reason: str) -> str:
    return f"{head}: {reason}" if reason else f"{head}."


def messages(filters: dict | None) -> list[str]:
    """Plain-English lines for the UI and the report (checks that need attention)."""
    if not isinstance(filters, dict):
        return []
    out: list[str] = []
    theme = filters.get("theme")
    if isinstance(theme, dict):
        text, reason = theme.get("text", ""), theme.get("reason", "")
        if theme.get("fit") == "no":
            out.append(_with_reason(f'Doesn\'t fit your theme "{text}"', reason))
        elif theme.get("fit") == "partly":
            out.append(_with_reason(f'Only partly fits your theme "{text}"', reason))
        elif theme.get("fit") != "yes":
            out.append("The AI didn't say if this fits your theme. Take a look.")
    for r in filters.get("rules") or []:
        if not isinstance(r, dict) or r.get("result") == "pass":
            continue
        label = UserRule(
            id="", kind=r.get("kind", ""), text=str(r.get("text", "")), mode=""
        ).label()
        if r.get("result") == "fail":
            out.append(_with_reason(f'Your rule "{label}" was not met', r.get("reason", "")))
        else:
            out.append(f'The AI didn\'t answer your rule "{label}". Take a look.')
    return out
