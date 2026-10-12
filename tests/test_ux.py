from __future__ import annotations

import pytest

from shuto.web import ux


@pytest.mark.parametrize(
    "counts,expected",
    [
        ({}, "empty"),
        ({"imported": 2}, "check"),
        ({"ready": 1, "approved": 5}, "check"),
        ({"needs_review": 1, "approved": 5}, "review"),
        ({"blocked": 1}, "review"),
        ({"described": 3}, "review"),
        ({"approved": 2}, "upload"),
        ({"prepared": 2, "uploaded": 1}, "upload"),
        ({"uploaded": 2}, "submit"),
        ({"exported": 2}, "submit"),
        ({"submitted": 2, "culled": 4}, "done"),
        ({"culled": 4}, "empty"),
    ],
)
def test_stage(counts, expected):
    assert ux.stage(counts) == expected


def test_next_action_table():
    assert ux.next_action("b1", {"imported": 40}) == {
        "label": "Check 40 new photos",
        "href": "/batch/b1/check",
    }
    assert ux.next_action("b1", {"needs_review": 12})["label"] == (
        "Review 12 photos that need a look"
    )
    assert ux.next_action("b1", {"described": 1})["label"] == "Review 1 photo"
    assert ux.next_action("b1", {"approved": 30})["label"] == "Upload 30 approved photos"
    assert ux.next_action("b1", {"uploaded": 1})["href"] == "/batch/b1/submit"
    assert ux.next_action("b1", {})["href"] == "/add"
    assert ux.next_action("b1", {"accepted": 1})["href"] == "/"


def test_next_action_quotes_batch_id():
    assert ux.next_action("a b/c", {"imported": 1})["href"] == "/batch/a%20b%2Fc/check"


def test_steps_states():
    def states(counts):
        return [s["state"] for s in ux.steps("b", counts)]

    assert states({"described": 1}) == ["done", "done", "current", "todo", "todo"]
    assert states({"imported": 1}) == ["done", "current", "todo", "todo", "todo"]
    assert states({}) == ["current", "todo", "todo", "todo", "todo"]
    assert states({"accepted": 1}) == ["done"] * 5
    assert [s["key"] for s in ux.steps("b", {})] == ["add", "check", "review", "upload", "submit"]
    assert ux.steps("b", {})[2]["href"] == "/batch/b/review"


def test_every_status_has_label_and_bucket():
    from shuto.models import Status

    for status in Status:
        assert ux.label(status)
        assert ux.bucket(status) in {"attention", "good", "aside", "stage", "done"}
    assert ux.bucket("needs_review") == "attention"
    assert ux.label("blocked") == "Can't send as is"


def test_friendly_error():
    e = ux.friendly_error(Exception("Ollama is not running at http://localhost:11434"))
    assert e["what"] == "The AI helper is not running."
    assert "Try again" in e["todo"]
    other = ux.friendly_error(Exception("weird thing"))
    assert other["what"] == "weird thing"
    assert other["todo"]
    assert ux.friendly_error("")["what"]


def test_friendly_error_upload_context():
    ai = ux.friendly_error("timed out (TimeoutError)")
    assert "AI" in ai["what"]
    up = ux.friendly_error("timed out (TimeoutError)", context="upload")
    assert "Shutterstock" in up["what"] and "AI" not in up["what"]
    refused = ux.friendly_error("connection refused", context="upload")
    assert "Shutterstock" in refused["what"]
    login = ux.friendly_error("530 Login incorrect", context="upload")
    assert "login" in login["what"]
    # a bare "530" or "confirm" is not enough
    for text in ("job 5300 failed", "please confirm your email"):
        assert ux.friendly_error(text, context="upload")["what"] == text
        assert ux.friendly_error(text)["what"] == text
    assert "access key" in ux.friendly_error("ANTHROPIC_API_KEY missing")["what"]
