"""Lambda entry points.

process_handler: SQS (S3 ObjectCreated events) -> Bedrock metadata -> optional FTPS upload
export_handler:  weekly schedule / manual invoke -> Shutterstock CSV per batch -> SNS notify

S3 layout:
  incoming/<batch>/<photo>.jpg     photos you upload (batch = e.g. 2026-W40)
  incoming/<batch>/notes.txt       optional context for the AI (place, event, season)
  metadata/<batch>/<photo>.json    generated metadata (review/edit freely)
  exports/<batch>/shutterstock.csv CSV to upload on submit.shutterstock.com
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.parse
from datetime import UTC, datetime

import boto3
from boto3.dynamodb.conditions import Attr, Key

from .csv_export import build_csv
from .ftps import DEFAULT_HOST, upload_file
from .metadata import generate_metadata

log = logging.getLogger()
log.setLevel(logging.INFO)

IMAGE_EXTS = (".jpg", ".jpeg")  # Shutterstock photos must be JPEG
INCOMING_PREFIX = "incoming/"

_s3 = boto3.client("s3")
_bedrock = boto3.client("bedrock-runtime")
_secrets = boto3.client("secretsmanager")
_sns = boto3.client("sns")
_table = boto3.resource("dynamodb").Table(os.environ.get("TABLE_NAME", "shuto-photos"))

_creds_cache: dict | None = None


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


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _ftps_creds() -> dict:
    global _creds_cache
    if _creds_cache is None:
        secret = _secrets.get_secret_value(SecretId=os.environ["FTPS_SECRET_ARN"])
        _creds_cache = json.loads(secret["SecretString"])
    return _creds_cache


def _batch_notes(bucket: str, batch: str) -> str:
    try:
        obj = _s3.get_object(Bucket=bucket, Key=f"{INCOMING_PREFIX}{batch}/notes.txt")
        return obj["Body"].read().decode("utf-8")[:1000]
    except _s3.exceptions.NoSuchKey:
        return ""


def _process_one(bucket: str, raw_key: str) -> None:
    key = urllib.parse.unquote_plus(raw_key)  # S3 event keys are URL-encoded
    parsed = parse_key(key)
    if not parsed:
        log.info("skip %s", key)
        return
    batch, filename = parsed

    existing = _table.get_item(Key={"batch": batch, "filename": filename}).get("Item")
    if existing and existing.get("status") in ("uploaded", "exported"):
        log.info("already processed %s/%s", batch, filename)
        return

    image = _s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    meta = generate_metadata(
        _bedrock,
        os.environ["BEDROCK_MODEL_ID"],
        filename,
        image,
        hints=_batch_notes(bucket, batch),
    )
    item = meta.to_dict()
    _s3.put_object(
        Bucket=bucket,
        Key=f"metadata/{batch}/{filename}.json",
        Body=json.dumps(item, ensure_ascii=False, indent=2).encode(),
        ContentType="application/json",
    )

    status = "described"
    if os.environ.get("UPLOAD_ENABLED", "false").lower() == "true":
        creds = _ftps_creds()
        upload_file(
            creds.get("host", DEFAULT_HOST), creds["username"], creds["password"], filename, image
        )
        status = "uploaded"

    _table.put_item(
        Item={
            **item,
            "batch": batch,
            "s3_key": key,
            "status": status,
            "updated_at": _now(),
        }
    )
    log.info("%s %s/%s warnings=%s", status, batch, filename, meta.warnings)


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
    pending = Attr("status").is_in(["described", "uploaded"])
    if batch:
        kwargs = {"KeyConditionExpression": Key("batch").eq(batch), "FilterExpression": pending}
        op = _table.query
    else:
        kwargs = {"FilterExpression": pending}
        op = _table.scan
    items: list[dict] = []
    while True:
        resp = op(**kwargs)
        items.extend(resp["Items"])
        if "LastEvaluatedKey" not in resp:
            return items
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def export_handler(event, _context):
    """Build exports/<batch>/shutterstock.csv for every batch with pending items.

    Invoke manually with {"batch": "2026-W40"} to export a single batch.
    """
    bucket = os.environ["BUCKET_NAME"]
    batch_filter = (event or {}).get("batch")
    by_batch: dict[str, list[dict]] = {}
    for it in _pending_items(batch_filter):
        by_batch.setdefault(it["batch"], []).append(it)

    written = []
    for batch, items in sorted(by_batch.items()):
        items.sort(key=lambda i: i["filename"])
        key = f"exports/{batch}/shutterstock-{int(time.time())}.csv"
        _s3.put_object(
            Bucket=bucket, Key=key, Body=build_csv(items).encode("utf-8"), ContentType="text/csv"
        )
        for it in items:
            _table.update_item(
                Key={"batch": batch, "filename": it["filename"]},
                UpdateExpression="SET #s = :s, exported_at = :t, export_key = :k",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":s": "exported", ":t": _now(), ":k": key},
            )
        written.append({"batch": batch, "count": len(items), "csv": f"s3://{bucket}/{key}"})

    if written and os.environ.get("TOPIC_ARN"):
        lines = [f"- {w['batch']}: {w['count']} photos -> {w['csv']}" for w in written]
        _sns.publish(
            TopicArn=os.environ["TOPIC_ARN"],
            Subject="Shuto: Shutterstock CSV ready",
            Message="New metadata CSVs:\n"
            + "\n".join(lines)
            + "\n\nUpload each CSV with the CSV button on submit.shutterstock.com.",
        )
    log.info("exported %s", written)
    return {"exported": written}
