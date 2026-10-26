"""Full-size quality numbers for photos checked earlier, and the table that compares them with
what Shutterstock decided."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PIL import ImageOps

from shuto.core import progress as core_progress
from shuto.core.config import Config
from shuto.core.models import QUALITY_CODES, REASON_LABELS, Photo, Status
from shuto.core.progress import Cancel, Progress
from shuto.ingest import cull as cull_mod
from shuto.ingest.imaging import open_image
from shuto.ingest.quality import QUALITY_VERSION
from shuto.store import Store

RESULT_STATUSES = (Status.ACCEPTED, Status.REJECTED)
OUTCOME_TEXT = {
    "accepted": "Accepted",
    "data_licensing": "Accepted for data licensing only",
    "rejected": "Rejected",
}
CROP_SIDE = 768  # longest side of the 100% crop shown on the photo page


def _needs_measuring(photo: Photo) -> bool:
    """A checked photo (it has a `cull` result) without the current quality numbers."""
    cull = photo.cull
    if not cull:
        return False
    return (cull.get("quality") or {}).get("version") != QUALITY_VERSION


def missing(store: Store, batch_id: str | None = None) -> list[Photo]:
    """Photos that `measure_missing` would look at, whatever their status."""
    return [p for p in store.list_photos(batch_id) if _needs_measuring(p)]


def measure_missing(
    cfg: Config,
    store: Store,
    batch_id: str | None = None,
    *,
    progress: Progress | None = None,
    cancel: Cancel | None = None,
) -> int:
    """Measure photos whose quality numbers are missing or from an older version.

    Only the `cull` result is updated (the numbers and warnings); the status and the original
    files are never touched. Photos whose file is gone or unreadable are skipped. Returns how
    many were measured.
    """
    progress = progress or core_progress.noop
    thresholds = cull_mod.CullThresholds(**cfg.cull.model_dump())
    photos = missing(store, batch_id)
    done = 0
    for n, photo in enumerate(photos, 1):
        if cancel and cancel():
            break
        progress(n, len(photos), photo.orig_name)
        if photo.id is None or not Path(photo.src_path).is_file():
            continue
        try:
            res = cull_mod.evaluate(photo.src_path, thresholds)
        except Exception:  # noqa: BLE001  (an unreadable photo is skipped, the rest go on)
            continue
        store.update_photo(
            photo.id, cull={**(photo.cull or {}), "quality": res.quality, "warnings": res.warnings}
        )
        done += 1
    return done


def _shown_problems(quality: dict[str, Any]) -> list[str]:
    return [p["text"] for p in quality.get("problems") or [] if p.get("shown")]


def calibration_rows(store: Store) -> list[dict[str, Any]]:
    """One row per photo with a recorded Shutterstock result, next to shuto's own numbers."""
    rows = []
    for photo in store.list_photos(None, RESULT_STATUSES):
        assert photo.id is not None
        codes = [r.reason_code for r in store.list_rejections(photo.id)]
        if photo.status == Status.REJECTED:
            outcome = "rejected"
        elif "quality_mild" in codes:
            outcome = "data_licensing"
        else:
            outcome = "accepted"
        quality = (photo.cull or {}).get("quality") or {}
        measured = quality.get("version") == QUALITY_VERSION
        rows.append(
            {
                "id": photo.id,
                "name": photo.orig_name,
                "measured": measured,
                "detail": quality.get("detail_p90") if measured else None,
                "ratio": quality.get("detail_ratio") if measured else None,
                "noise": quality.get("flat_noise") if measured else None,
                "problems": _shown_problems(quality) if measured else [],
                "outcome": outcome,
                "codes": codes,
                "verdict": OUTCOME_TEXT[outcome]
                + (
                    ": " + ", ".join(REASON_LABELS.get(c, c) for c in codes)
                    if codes and outcome == "rejected"
                    else ""
                ),
                "quality_reject": outcome != "accepted" and any(c in QUALITY_CODES for c in codes),
            }
        )
    return rows


def calibration_summary(rows: list[dict[str, Any]]) -> dict[str, int]:
    """How often shuto's warning matched Shutterstock's quality verdict (measured photos only)."""
    measured = [r for r in rows if r["measured"]]
    flagged = [r for r in measured if r["problems"]]
    return {
        "photos": len(rows),
        "measured": len(measured),
        "quality_rejects": sum(1 for r in measured if r["quality_reject"]),
        "warned_and_rejected": sum(1 for r in flagged if r["quality_reject"]),
        "warned_but_fine": sum(1 for r in flagged if not r["quality_reject"]),
        "missed": sum(1 for r in measured if r["quality_reject"] and not r["problems"]),
    }


def crop_best_tile(cfg: Config, photo: Photo) -> Path | None:
    """A JPEG of the sharpest spot of the photo at 100% (what Shutterstock sees), or None.

    Cut from the original (only read) and cached in the workspace per photo and quality version.
    """
    quality = (photo.cull or {}).get("quality") or {}
    tile = quality.get("best_tile")
    src = Path(photo.src_path)
    if not tile or len(tile) != 4 or not src.is_file():
        return None
    dest = cfg.workspace_path("crops", f"{photo.sha256}-v{quality.get('version')}.jpg")
    if dest.is_file():
        return dest
    try:
        with open_image(src) as im:
            im = ImageOps.exif_transpose(im)
            x, y, w, h = (float(v) for v in tile)
            side_w, side_h = min(im.width, CROP_SIDE), min(im.height, CROP_SIDE)
            left = min(max(0, round((x + w / 2) * im.width - side_w / 2)), im.width - side_w)
            top = min(max(0, round((y + h / 2) * im.height - side_h / 2)), im.height - side_h)
            box = (left, top, left + side_w, top + side_h)
            crop = im.crop(box).convert("RGB")
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".tmp")
        crop.save(tmp, format="JPEG", quality=90)
        tmp.replace(dest)
    except Exception:  # noqa: BLE001  (unreadable photo: no crop)
        return None
    return dest
