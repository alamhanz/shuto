"""Live speed check for a local describe batch.

After every `CHECK_EVERY` described photos the running average (seconds per photo) is compared
with the expected seconds per photo shown before the run. At `SLOW_FACTOR` times that or more,
a suggestion is offered: fewer tries per photo, or a faster installed model. The choices apply
to the photos not yet started, at once. The describe loop (worker threads) and the web routes
share one `LiveRun`; every read and write goes through its lock.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

SLOW_FACTOR = 1.3  # slow = average >= 1.3 x expected (a 30% buffer)
CHECK_EVERY = 5  # look after 5, 10, 15, ... described photos
SUGGESTED_TRIES = 3  # the lower "max tries" that is offered
MAX_MODEL_OPTIONS = 3  # installed models offered at most


def running_average(seconds: list[float]) -> float:
    """Mean seconds per photo (0.0 when nothing was timed yet)."""
    return sum(seconds) / len(seconds) if seconds else 0.0


def is_slow(average: float, expected: float) -> bool:
    """True when the average is at least SLOW_FACTOR times the expected seconds per photo."""
    return expected > 0 and average >= SLOW_FACTOR * expected


def is_check_due(described: int) -> bool:
    """True at every CHECK_EVERY-th described photo."""
    return described > 0 and described % CHECK_EVERY == 0


def suggested_tries(current: int) -> int | None:
    """A lower max tries to offer, or None when the photo already gets a single try."""
    if current <= 1:
        return None
    return SUGGESTED_TRIES if current > SUGGESTED_TRIES else current - 1


@dataclass(frozen=True)
class ModelOption:
    """A faster model that fits this machine. `installed` False = must be downloaded first."""

    tag: str
    seconds: float
    installed: bool


@dataclass(frozen=True)
class Suggestion:
    average: float
    expected: float
    tries: int | None  # offered lower max tries
    models: tuple[ModelOption, ...]  # installed first
    current_tries: int
    current_model: str

    @property
    def installed_models(self) -> tuple[ModelOption, ...]:
        return tuple(m for m in self.models if m.installed)

    @property
    def missing_models(self) -> tuple[ModelOption, ...]:
        return tuple(m for m in self.models if not m.installed)


OptionsFn = Callable[[str], "tuple[float, list[ModelOption]]"]
LlmFactory = Callable[[str], Any]


@dataclass
class LiveRun:
    """Shared, mutable settings of one running local describe batch (thread-safe)."""

    model: str
    max_tries: int
    expected_s: float
    options: OptionsFn | None = None  # model -> (expected seconds for it, faster options)
    llm_factory: LlmFactory | None = None  # tag -> provider, used after a switch
    notice: str = ""
    _described: int = 0
    _window: list[float] = field(default_factory=list)
    _dismissed_avg: float | None = None
    _suggestion: Suggestion | None = None
    _llms: dict[str, Any] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # ---- describe loop side

    def snapshot(self) -> tuple[str, int]:
        """The model and max tries to use for the next photo."""
        with self._lock:
            return self.model, self.max_tries

    def llm_for(self, tag: str, default: Any) -> Any:
        """The provider for `tag` (built once); `default` when it is the batch's own model."""
        if getattr(default, "model", None) == tag:
            return default
        with self._lock:
            found = self._llms.get(tag)
        if found is not None:
            return found
        if self.llm_factory is None:
            return default
        made = self.llm_factory(tag)
        with self._lock:
            return self._llms.setdefault(tag, made)

    def record(self, seconds: float) -> Suggestion | None:
        """A photo was described in `seconds`; at every CHECK_EVERY-th one, look at the speed."""
        with self._lock:
            self._described += 1
            self._window.append(max(0.0, float(seconds)))
            if not is_check_due(self._described):
                return self._suggestion
            average = running_average(self._window)
            if not is_slow(average, self.expected_s):
                self._suggestion = None
                return None
            if self._dismissed_avg is not None and average <= self._dismissed_avg:
                return self._suggestion
            model, tries, expected = self.model, self.max_tries, self.expected_s
        options = self.options
        models: list[ModelOption] = []
        if options is not None:
            try:
                models = options(model)[1]
            except Exception:
                models = []
        installed_first = sorted(models, key=lambda m: not m.installed)
        shown = [m for m in installed_first if m.installed][:MAX_MODEL_OPTIONS]
        shown += [m for m in installed_first if not m.installed][:1]
        tries_offer = suggested_tries(tries)
        with self._lock:
            if tries_offer is None and not shown:
                self._suggestion = None
                return None
            self._suggestion = Suggestion(
                average, expected, tries_offer, tuple(shown), tries, model
            )
            self._dismissed_avg = None
            return self._suggestion

    # ---- web side

    def suggestion(self) -> Suggestion | None:
        with self._lock:
            return self._suggestion

    def set_max_tries(self, tries: int) -> None:
        """Fewer (or more) tries for the photos not started yet."""
        with self._lock:
            self.max_tries = max(1, int(tries))
            self.notice = (
                f"The rest of this batch now uses up to {self.max_tries} "
                f"{'try' if self.max_tries == 1 else 'tries'} per photo."
            )
            self._restart_window()

    def switch_model(self, tag: str) -> bool:
        """Use `tag` for the photos not started yet. Only models offered in the suggestion
        (installed and fitting this machine) are accepted."""
        with self._lock:
            offered = self._suggestion.installed_models if self._suggestion else ()
            pick = next((m for m in offered if m.tag == tag), None)
            if pick is None:
                return False
            self.model = pick.tag
            self.expected_s = pick.seconds
            self.notice = f"The rest of this batch now uses {pick.tag}."
            self._restart_window()
            return True

    def dismiss(self) -> None:
        """Keep going. Ask again only when a later check is slow and worse than this one."""
        with self._lock:
            if self._suggestion is not None:
                self._dismissed_avg = self._suggestion.average
            self._suggestion = None
            self.notice = ""

    def _restart_window(self) -> None:
        self._window = []
        self._dismissed_avg = None
        self._suggestion = None
