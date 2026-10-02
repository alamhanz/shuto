import csv
import io
import json

import boto3
import pytest
from moto import mock_aws
from PIL import Image

from shuto import handlers

BUCKET = "shuto-test"
TABLE = "shuto-photos"


def _jpeg(w=2400, h=1800) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (90, 140, 60)).save(buf, "JPEG")
    return buf.getvalue()


class FakeBedrock:
    def __init__(self, editorial=False):
        self.calls = 0
        self.editorial = editorial

    def converse(self, **_kwargs):
        self.calls += 1
        body = {
            "description": "Red torii gates at Fushimi Inari shrine in Kyoto, Japan",
            "keywords": ["torii", "kyoto", "japan", "shrine", "red", "travel", "asia", "gate"],
            "categories": ["Buildings/Landmarks", "Religion"],
            "editorial": self.editorial,
        }
        return {"output": {"message": {"content": [{"text": json.dumps(body)}]}}}


class FakeSNS:
    def __init__(self):
        self.messages = []

    def publish(self, **kwargs):
        self.messages.append(kwargs["Message"])


@pytest.fixture
def env(monkeypatch):
    with mock_aws():
        monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-northeast-1")
        monkeypatch.setenv("TABLE_NAME", TABLE)
        monkeypatch.setenv("BUCKET_NAME", BUCKET)
        monkeypatch.setenv("TOPIC_ARN", "arn:aws:sns:ap-northeast-1:123456789012:shuto")
        monkeypatch.setenv("BEDROCK_MODEL_ID", "jp.anthropic.claude-sonnet-4-6")
        monkeypatch.setenv("UPLOAD_ENABLED", "false")
        s3 = boto3.client("s3", region_name="ap-northeast-1")
        s3.create_bucket(
            Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "ap-northeast-1"}
        )
        boto3.client("dynamodb", region_name="ap-northeast-1").create_table(
            TableName=TABLE,
            KeySchema=[
                {"AttributeName": "batch", "KeyType": "HASH"},
                {"AttributeName": "filename", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "batch", "AttributeType": "S"},
                {"AttributeName": "filename", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        for fn in (handlers._s3, handlers._table, handlers._ftps_creds):
            fn.cache_clear()

        bedrock, sns, uploads = FakeBedrock(), FakeSNS(), []
        monkeypatch.setattr(handlers, "_bedrock", lambda: bedrock)
        monkeypatch.setattr(handlers, "_sns", lambda: sns)
        monkeypatch.setattr(handlers, "_ftps_creds", lambda: {"username": "u", "password": "p"})
        monkeypatch.setattr(
            handlers, "upload_file", lambda host, user, pw, name, data: uploads.append((name, data))
        )

        class Env:
            pass

        e = Env()
        e.s3, e.bedrock, e.sns, e.uploads, e.monkeypatch = s3, bedrock, sns, uploads, monkeypatch
        yield e
        for fn in (handlers._s3, handlers._table):
            fn.cache_clear()


def _put_and_process(e, key, data=None):
    e.s3.put_object(Bucket=BUCKET, Key=key, Body=data or _jpeg())
    event_key = key.replace(" ", "+")  # S3 events URL-encode keys
    body = {"Records": [{"s3": {"bucket": {"name": BUCKET}, "object": {"key": event_key}}}]}
    return handlers.process_handler(
        {"Records": [{"messageId": "m1", "body": json.dumps(body)}]}, None
    )


def _item(batch, name):
    return handlers._table().get_item(Key={"batch": batch, "filename": name}).get("Item")


def _csv_rows(e, key):
    body = e.s3.get_object(Bucket=BUCKET, Key=key)["Body"].read().decode()
    return list(csv.DictReader(io.StringIO(body)))


def _csv_keys(e, batch):
    resp = e.s3.list_objects_v2(Bucket=BUCKET, Prefix=f"exports/{batch}/")
    return sorted(o["Key"] for o in resp.get("Contents", []))


def test_dry_run_describes_and_exports(env):
    assert _put_and_process(env, "incoming/2026-W40/my photo.JPG") == {"batchItemFailures": []}
    item = _item("2026-W40", "my photo.JPG")
    assert item["status"] == "described" and "uploaded_at" not in item
    assert env.uploads == []

    handlers.export_handler({}, None)
    (key,) = _csv_keys(env, "2026-W40")
    rows = _csv_rows(env, key)
    assert rows[0]["Filename"] == "my photo.JPG"
    assert rows[0]["Categories"] == "Buildings/Landmarks,Religion"
    assert "exported_at" in _item("2026-W40", "my photo.JPG")
    assert "2026-W40 commercial: 1 photos" in env.sns.messages[0]

    # nothing pending -> no new CSV, no email
    handlers.export_handler({}, None)
    assert len(_csv_keys(env, "2026-W40")) == 1 and len(env.sns.messages) == 1


def test_edited_json_is_used_for_export(env):
    _put_and_process(env, "incoming/b1/a.jpg")
    meta_key = "metadata/b1/a.jpg.json"
    data = json.loads(env.s3.get_object(Bucket=BUCKET, Key=meta_key)["Body"].read())
    data["description"] = "My own better title"
    env.s3.put_object(Bucket=BUCKET, Key=meta_key, Body=json.dumps(data).encode())

    handlers.export_handler({"batch": "b1"}, None)
    (key,) = _csv_keys(env, "b1")
    assert _csv_rows(env, key)[0]["Description"] == "My own better title"


def test_dry_run_then_enable_uploads(env):
    _put_and_process(env, "incoming/b1/a.jpg")
    handlers.export_handler({}, None)
    assert env.bedrock.calls == 1

    env.monkeypatch.setenv("UPLOAD_ENABLED", "true")
    _put_and_process(env, "incoming/b1/a.jpg")  # upload the same photo again
    assert env.bedrock.calls == 1  # reused stored JSON, no second model call
    ((name, data),) = env.uploads
    assert name == "a.jpg" and b"Fushimi Inari" in data  # metadata embedded in the JPEG
    item = _item("b1", "a.jpg")
    assert item["status"] == "uploaded" and "exported_at" not in item

    handlers.export_handler({}, None)
    assert len(_csv_keys(env, "b1")) == 2  # dry-run preview + real CSV

    _put_and_process(env, "incoming/b1/a.jpg")  # already on Shutterstock -> skipped
    assert len(env.uploads) == 1


def test_upload_mode_exports_only_uploaded_photos(env):
    env.monkeypatch.setenv("UPLOAD_ENABLED", "true")
    _put_and_process(env, "incoming/b1/ok.jpg")

    def boom(*_args):
        raise OSError("ftps down")

    env.monkeypatch.setattr(handlers, "upload_file", boom)
    result = _put_and_process(env, "incoming/b1/fail.jpg")
    assert result == {"batchItemFailures": [{"itemIdentifier": "m1"}]}

    handlers.export_handler({}, None)
    (key,) = _csv_keys(env, "b1")
    assert [r["Filename"] for r in _csv_rows(env, key)] == ["ok.jpg"]


def test_too_small_is_skipped_and_reported_once(env):
    _put_and_process(env, "incoming/b1/tiny.jpg", _jpeg(1000, 1000))
    assert env.bedrock.calls == 0
    assert _item("b1", "tiny.jpg")["status"] == "too_small"

    handlers.export_handler({}, None)
    assert "tiny.jpg: 1.0 MP, below 4 MP" in env.sns.messages[0]
    assert _csv_keys(env, "b1") == []
    handlers.export_handler({}, None)
    assert len(env.sns.messages) == 1


def test_editorial_goes_to_separate_csv(env):
    _put_and_process(env, "incoming/b1/street.jpg")
    env.bedrock.editorial = True
    _put_and_process(env, "incoming/b1/festival.jpg")

    handlers.export_handler({}, None)
    keys = _csv_keys(env, "b1")
    assert len(keys) == 2
    editorial = [k for k in keys if "editorial" in k][0]
    assert [r["Filename"] for r in _csv_rows(env, editorial)] == ["festival.jpg"]
    assert "editorial: festival.jpg" in env.sns.messages[0]


def test_broken_json_is_reported_not_exported(env):
    _put_and_process(env, "incoming/b1/a.jpg")
    env.s3.put_object(Bucket=BUCKET, Key="metadata/b1/a.jpg.json", Body=b"{oops")
    handlers.export_handler({}, None)
    assert _csv_keys(env, "b1") == []
    assert "unreadable metadata JSON" in env.sns.messages[0]
    assert "exported_at" not in _item("b1", "a.jpg")


def test_ignores_non_photo_keys(env):
    assert _put_and_process(env, "incoming/b1/notes.txt", b"Kyoto") == {"batchItemFailures": []}
    assert env.bedrock.calls == 0
