"""Time (local) and cost (API) estimates shown before a describe run."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal


@dataclass
class Estimate:
    kind: Literal["time", "cost"]
    seconds: float | None
    usd: float | None
    basis: str
    per_image: float
    n: int
    text: str

    def to_dict(self) -> dict:
        return asdict(self)


def cost_per_image(
    api: Any, prompt_tokens: int = 1200, output_tokens: int = 600, image_tokens: int | None = None
) -> float:
    tin = (image_tokens if image_tokens is not None else api.image_tokens) + prompt_tokens
    return (tin * api.usd_per_mtok_in + output_tokens * api.usd_per_mtok_out) / 1_000_000


def format_duration(seconds: float) -> str:
    if seconds < 90:
        return f"{max(1, round(seconds))} s"
    if seconds < 5400:
        return f"{round(seconds / 60)} min"
    h = seconds / 3600
    return f"{h:.1f} h"


def for_batch(
    n: int,
    provider: str,
    model: str,
    *,
    recommendation: Any = None,
    api_model: Any = None,
    concurrency: int = 1,
    image_side: int = 1568,
    prompt_tokens: int = 1200,
    output_tokens: int = 600,
) -> Estimate:
    noun = "photo" if n == 1 else "photos"
    if provider == "ollama":
        if recommendation is None:
            return Estimate(
                "time",
                None,
                None,
                "unknown",
                0.0,
                n,
                f"time unknown for {n} {noun} (no estimate for {model})",
            )
        per = float(recommendation.est_seconds_per_image)
        total = per * n / max(1, concurrency)
        basis = recommendation.basis
        return Estimate(
            "time", total, None, basis, per, n,
            f"~{format_duration(total)} for {n} {noun} ({basis}, {per:.0f} s/photo)",
        )  # fmt: skip
    if provider == "fake":
        return Estimate("time", 0.0, 0.0, "fake", 0.0, n, f"instant for {n} {noun} (fake provider)")
    if api_model is None:
        return Estimate(
            "cost",
            None,
            None,
            "unknown",
            0.0,
            n,
            f"cost unknown for {n} {noun} ({provider}/{model})",
        )
    # image tokens scale roughly with pixel area, capped at the model's 1568 px reference
    scale = min(1.0, (image_side / 1568) ** 2) if image_side else 1.0
    per = cost_per_image(
        api_model, prompt_tokens, output_tokens, int(api_model.image_tokens * scale)
    )
    usd = per * n
    basis = f"pricing table {api_model.updated}" if api_model.updated else "pricing table"
    approx = "~" if usd >= 0.01 else "<"
    amount = f"${usd:.2f}" if usd >= 0.01 else "$0.01"
    return Estimate("cost", None, usd, basis, per, n, f"{approx}{amount} for {n} {noun} ({basis})")
