"""Lambda entry points.

process_handler: SQS (S3 ObjectCreated events) -> Bedrock metadata -> optional FTPS upload
export_handler:  weekly schedule / manual invoke -> Shutterstock CSVs per batch -> SNS notify

S3 layout:
  incoming/<batch>/<photo>.jpg       photos you upload (batch = e.g. 2026-W40)
  incoming/<batch>/notes.txt         optional context for the AI (place, event, season)
  metadata/<batch>/<photo>.json      generated metadata = source of truth (edit freely;
                                     delete it to make Claude regenerate)
  exports/<batch>/shutterstock-<ts>.csv            commercial photos
  exports/<batch>/shutterstock-editorial-<ts>.csv  editorial photos (review captions first)

Photo state in DynamoDB (pk=batch, sk=filename):
  status       described | uploaded | too_small
  uploaded_at  set when the FTPS upload succeeded
  exported_at  set when the photo was written to a CSV (cleared when re-uploaded)
"""

from __future__ import annotations

import functools
import json
import logging
import os
import urllib.parse
from datetime import UTC, datetime

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from .csv_export import build_csv
from .embed import embed_metadata
from .ftps import DEFAULT_HOST, upload_file
from .metadata import MIN_MEGAPIXELS, generate_metadata, inspect_image, normalize

log = logging.getLogger()
log.setLevel(logging.INFO)

IMAGE_EXTS = (".jpg", ".jpeg")  # Shutterstock photos must be JPEG
INCOMING_PREFIX = "incoming/"


# Clients are created lazily so tests (moto) and cold starts stay cheap.
@functools.cache
def _s3():
    return boto3.client("s3")


@functools.cache
def _bedrock():
    return boto3.client("bedrock-runtime")


@functools.cache
def _sns():
    return boto3.client("sns")


@functools.cache
def _table():
    return boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])


@functools.cache
def _ftps_creds() -> dict:
    sm = boto3.client("secretsmanager")
    return json.loads(sm.get_secret_value(SecretId=os.environ["FTPS_SECRET_ARN"])["SecretString"])


def _upload_enabled() -> bool:
    return os.environ.get("UPLOAD_ENABLED", "false").lower() == "true"


def parse_key(key: str) -> tuple[str, str] | None:
    """incoming/<batch>/<file>.jpg -> (batch, file); None if not a photo we handle."""
    if not key.startswith(INCOMING_PREFIX):
        return None
    parts = key[len(INCOMING_PREFIX) :].split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return None
    if not parts[1].lower().endswith(IMAGE_EXTS):
        return None
    return parts[0], parts[1]


def metadata_key(batch: str, filename: str) -> str:
    return f"metadata/{batch}/{filename}.json"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _get_text(bucket: str, key: str) -> str | None:
    try:
        return _s3().get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")
    except ClientError as e:
        if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
            return None
        raise


def _update(batch: str, filename: str, set_fields: dict, remove: tuple[str, ...] = ()) -> None:
    names = {f"#{k}": k for k in set_fields}
    expr = "SET " + ", ".join(f"#{k} = :{k}" for k in set_fields)
    if remove:
        expr += " REMOVE " + ", ".join(remove)
    _table().update_item(
        Key={"batch": batch, "filename": filename},
        UpdateExpression=expr,
        ExpressionAttributeNames=names,
        ExpressionAttributeValues={f":{k}": v for k, v in set_fields.items()},
    )


def _process_one(bucket: str, raw_key: str) -> None:
    key = urllib.parse.unquote_plus(raw_key)  # S3 event keys are URL-encoded
    parsed = parse_key(key)
    if not parsed:
        log.info("skip %s", key)
        return
    batch, filename = parsed

    existing = _table().get_item(Key={"batch": batch, "filename": filename}).get("Item") or {}
    if existing.get("uploaded_at"):
        log.info("already on Shutterstock: %s/%s", batch, filename)
        return

    image = _s3().get_object(Bucket=bucket, Key=key)["Body"].read()
    info = inspect_image(image)
    base = {"s3_key": key, "megapixels": f"{info.megapixels:.1f}", "updated_at": _now()}

    if info.megapixels < MIN_MEGAPIXELS:
        _update(batch, filename, {**base, "status": "too_small"})
        log.warning("too small (%.1f MP): %s/%s", info.megapixels, batch, filename)
        return

    # The JSON in S3 is the source of truth: reuse it (keeps your edits, saves Bedrock calls).
    meta_key = metadata_key(batch, filename)
    stored = _get_text(bucket, meta_key)
    if stored is not None:
        meta = normalize(filename, json.loads(stored))
    else:
        meta = generate_metadata(
            _bedrock(),
            os.environ["BEDROCK_MODEL_ID"],
            filename,
            image,
            hints=(_get_text(bucket, f"{INCOMING_PREFIX}{batch}/notes.txt") or "")[:1000],
            captured=info.captured,
        )
        _s3().put_object(
            Bucket=bucket,
            Key=meta_key,
            Body=json.dumps(meta.to_dict(), ensure_ascii=False, indent=2).encode(),
            ContentType="application/json",
        )

    fields = {
        **base,
        "status": "described",
        "metadata_key": meta_key,
        "editorial": meta.editorial,
        "warnings": meta.warnings,
    }
    if not _upload_enabled():
        _update(batch, filename, fields)
        log.info("described %s/%s warnings=%s", batch, filename, meta.warnings)
        return

    tagged = embed_metadata(image, meta.description, meta.description, meta.keywords)
    creds = _ftps_creds()
    upload_file(
        creds.get("host", DEFAULT_HOST), creds["username"], creds["password"], filename, tagged
    )
    # Clear any dry-run export so the photo lands in the next CSV.
    _update(
        batch,
        filename,
        {**fields, "status": "uploaded", "uploaded_at": _now()},
        remove=("exported_at", "export_keys"),
    )
    log.info("uploaded %s/%s warnings=%s", batch, filename, meta.warnings)


def process_handler(event, _context):
    """SQS batch of S3 notifications. Reports per-message failures for retry/DLQ."""
    failures = []
    for record in event.get("Records", []):
        try:
            body = json.loads(record["body"])
            for s3rec in body.get("Records", []):  # s3:TestEvent has no Records
                _process_one(s3rec["s3"]["bucket"]["name"], s3rec["s3"]["object"]["key"])
        except Exception:
            log.exception("failed message %s", record.get("messageId"))
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}


def _pending_items(batch: str | None) -> list[dict]:
    # With uploads on, only export photos that are actually on Shutterstock.
    # "too_small" photos are included once so the email can report them.
    ready = ["uploaded", "too_small"]
    if not _upload_enabled():
        ready.append("described")
    pending = Attr("status").is_in(ready) & Attr("exported_at").not_exists()
    if batch:
        kwargs = {"KeyConditionExpression": Key("batch").eq(batch), "FilterExpression": pending}
        op = _table().query
    else:
        kwargs = {"FilterExpression": pending}
        op = _table().scan
    items: list[dict] = []
    while True:
        resp = op(**kwargs)
        items.extend(resp["Items"])
        if "LastEvaluatedKey" not in resp:
            return items
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def export_handler(event, _context):
    """Write CSVs for every batch with pending photos, then email a summary.

    Invoke manually with {"batch": "2026-W40"} to export a single batch.
    """
    bucket = os.environ["BUCKET_NAME"]
    batch_filter = (event or {}).get("batch")
    by_batch: dict[str, list[dict]] = {}
    for it in _pending_items(batch_filter):
        by_batch.setdefault(it["batch"], []).append(it)

    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")  # unique, sortable
    report: list[str] = []
    problems: list[str] = []
    for batch, items in sorted(by_batch.items()):
        groups: dict[str, list[dict]] = {"commercial": [], "editorial": []}
        exported: list[str] = []
        for it in sorted(items, key=lambda i: i["filename"]):
            name = it["filename"]
            if it["status"] == "too_small":
                problems.append(f"{batch}/{name}: {it.get('megapixels')} MP, below 4 MP - skipped")
                _update(batch, name, {"exported_at": _now(), "export_keys": []})
                continue
            raw = _get_text(bucket, it.get("metadata_key") or metadata_key(batch, name))
            try:
                meta = normalize(name, json.loads(raw or ""))
            except (ValueError, TypeError) as e:
                problems.append(f"{batch}/{name}: unreadable metadata JSON ({e})")
                continue
            if meta.warnings:
                problems.append(f"{batch}/{name}: {', '.join(meta.warnings)}")
            groups["editorial" if meta.editorial else "commercial"].append(meta.to_dict())
            exported.append(name)

        keys = []
        for kind, rows in groups.items():
            if not rows:
                continue
            suffix = "-editorial" if kind == "editorial" else ""
            key = f"exports/{batch}/shutterstock{suffix}-{ts}.csv"
            _s3().put_object(
                Bucket=bucket, Key=key, Body=build_csv(rows).encode("utf-8"), ContentType="text/csv"
            )
            keys.append(key)
            report.append(f"- {batch} {kind}: {len(rows)} photos -> s3://{bucket}/{key}")
            if kind == "editorial":
                report.extend(f"    editorial: {r['filename']}" for r in rows)

        for name in exported:
            _update(batch, name, {"exported_at": _now(), "export_keys": keys})

    if (report or problems) and os.environ.get("TOPIC_ARN"):
        msg = "New Shutterstock CSVs:\n" + ("\n".join(report) or "(none)")
        if problems:
            msg += "\n\nNeeds attention:\n" + "\n".join(f"- {p}" for p in problems)
        msg += (
            "\n\nUpload each CSV with the CSV button on submit.shutterstock.com, then submit."
            "\nCheck editorial captions (CITY, COUNTRY - Month D, YYYY: ...) before submitting."
        )
        _sns().publish(TopicArn=os.environ["TOPIC_ARN"], Subject="Shuto: CSV ready", Message=msg)
    log.info("export report=%s problems=%s", report, problems)
    return {"exported": report, "problems": problems}
