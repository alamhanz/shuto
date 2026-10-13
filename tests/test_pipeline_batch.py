from __future__ import annotations

import ftplib

import pytest

from shuto import local_models, pipeline, secrets
from shuto.llm.fake import FakeProvider
from shuto.models import Status

from .test_pipeline import FakeFTP


def _import(cfg, store, photos_dir, **kw):
    imp = pipeline.import_folder(cfg, store, photos_dir, **kw)
    (batch,) = imp.batches
    return batch


def _approved(cfg, store, photos_dir):
    batch = _import(cfg, store, photos_dir)
    pipeline.check_batch(cfg, store, batch)
    pipeline.approve(cfg, store, batch_id=batch)
    return batch


def test_batch_name_overrides_batch_by(cfg, store, photos_dir):
    sub = photos_dir / "sub"
    sub.mkdir()
    (sub / "x.jpg").write_bytes((photos_dir / "a.jpg").read_bytes() + b"\0")
    imp = pipeline.import_folder(cfg, store, photos_dir, batch_name="Trip to Kyoto!")
    assert set(imp.batches) == {"Trip-to-Kyoto"}
    assert {p.batch_id for p in store.list_photos()} == {"Trip-to-Kyoto"}


def test_check_batch_combined_progress(cfg, store, photos_dir):
    batch = _import(cfg, store, photos_dir)
    seen = []
    summary = pipeline.check_batch(
        cfg, store, batch, progress=lambda d, t, m: seen.append((d, t, m))
    )
    assert summary.cull.ready == 3 and summary.cull.culled == 2
    assert summary.describe is not None and summary.describe.described == 3
    total = pipeline.CHECK_SCALE
    dones = [d for d, _, _ in seen]
    assert dones == sorted(dones) and {t for _, t, _ in seen} == {total}
    quality = [m for _, _, m in seen if m.startswith("Step 1 of 2 · Quality check")]
    ai = [m for _, _, m in seen if m.startswith("Step 2 of 2 · Writing titles & keywords")]
    # the quality check runs 0/5 .. 5/5, then the AI starts at 0/3 (photos left after culling)
    assert quality[0].endswith(" 0/5") and quality[-1].startswith("Step 1 of 2 · Quality check 5/5")
    assert ai[0].endswith(" 0/3")
    counters = [m.split("titles & keywords ")[1].split(" ")[0] for m in ai]
    assert counters == ["0/3", "1/3", "2/3", "3/3"]
    assert "left" not in ai[0] and "left" in ai[1] and "left" in ai[2]  # once one is done
    # the bar is only full with the last photo described
    full = [i for i, (d, _, _) in enumerate(seen) if d == total]
    assert full == [len(seen) - 1] and seen[-1][2].endswith("3/3")
    first_ai = next(i for i, (_, _, m) in enumerate(seen) if m.startswith("Step 2"))
    assert seen[first_ai][0] == total // 10


def test_check_batch_cancel_skips_describe(cfg, store, photos_dir):
    batch = _import(cfg, store, photos_dir)
    summary = pipeline.check_batch(cfg, store, batch, cancel=lambda: True)
    assert summary.describe is None
    assert not store.list_photos(batch, [Status.DESCRIBED])


def test_check_batch_preflight_before_work(cfg, store, photos_dir, monkeypatch):
    batch = _import(cfg, store, photos_dir)
    monkeypatch.setattr(pipeline, "describe_preflight", lambda *a, **k: ["Ollama is not running."])
    with pytest.raises(pipeline.PipelineError, match="Ollama"):
        pipeline.check_batch(cfg, store, batch)
    assert len(store.list_photos(batch, [Status.IMPORTED])) == 5

    cfg_ollama = cfg.model_copy(update={"llm": cfg.llm.model_copy(update={"provider": "ollama"})})
    monkeypatch.undo()
    monkeypatch.setattr(pipeline, "model_choice_path", lambda: photos_dir / "no-choice.json")
    with pytest.raises(local_models.ModelChoiceRequired):
        pipeline.check_batch(cfg_ollama, store, batch)
    assert len(store.list_photos(batch, [Status.IMPORTED])) == 5


def test_estimate_check_counts_imported_and_ready(cfg, store, photos_dir):
    batch = _import(cfg, store, photos_dir)
    est = pipeline.estimate_check(cfg, store, batch)
    assert est.n == 5 and est.text.startswith("up to ")
    pipeline.cull_batch(cfg, store, batch)
    assert pipeline.estimate_check(cfg, store, batch).n == 3


def test_style_overrides_reach_prompt(cfg, store, photos_dir):
    batch = _import(cfg, store, photos_dir)
    pipeline.set_ui_setting(
        store,
        "style",
        {"theme": "Quiet Hokkaido", "always_include": "hokkaido, snow", "tone": "calm"},
    )
    styled = pipeline.apply_style_overrides(cfg, store)
    assert styled.style.theme == "Quiet Hokkaido"
    assert styled.style.keywords.always_include == ["hokkaido", "snow"]
    assert cfg.style.theme == ""  # original untouched
    fake = FakeProvider("fake")
    pipeline.check_batch(styled, store, batch, llm=fake)
    assert fake.calls
    text = fake.calls[0].system + fake.calls[0].prompt
    assert "Quiet Hokkaido" in text and "hokkaido" in text


def test_style_without_override_is_same_config(cfg, store):
    assert pipeline.apply_style_overrides(cfg, store) is cfg


def test_ui_settings_validation(store):
    assert pipeline.get_ui_setting(store, "onboarded", False) is False
    pipeline.set_ui_setting(store, "ui.onboarded", True)
    assert pipeline.get_ui_setting(store, "onboarded") is True
    pipeline.set_ui_setting(store, "last_folder", "/photos")
    assert store.get_setting("ui.last_folder") == "/photos"
    with pytest.raises(pipeline.PipelineError):
        pipeline.set_ui_setting(store, "nope", 1)
    with pytest.raises(pipeline.PipelineError):
        pipeline.set_ui_setting(store, "style", {"theme": "x" * 201})
    with pytest.raises(pipeline.PipelineError):
        pipeline.set_ui_setting(store, "style", {"tone": "y" * 201})
    with pytest.raises(pipeline.PipelineError):
        pipeline.set_ui_setting(store, "style", {"always_include": [f"w{i}" for i in range(21)]})
    stored = pipeline.set_ui_setting(store, "style", {"theme": " ok ", "junk": 1})
    assert stored == {"theme": "ok"}


def test_bulk_edit_merges_and_refuses(cfg, store, photos_dir):
    batch = _import(cfg, store, photos_dir)
    pipeline.check_batch(cfg, store, batch)
    described = [p.id for p in store.list_photos(batch, [Status.DESCRIBED, Status.NEEDS_REVIEW])]
    culled = [p.id for p in store.list_photos(batch, [Status.CULLED])]
    assert described and culled
    res = pipeline.bulk_edit(
        cfg,
        store,
        [*described, *culled, 9999],
        add_keywords=["Sakura", "sakura", " zzz-new "],
        remove_keywords=["JAPAN"],
        categories=["Nature"],
        editorial=True,
    )
    assert sorted(res.changed) == sorted(described)
    assert set(res.refused) == {*culled, 9999}
    assert all("Set aside" in res.refused[i] for i in culled)
    meta = store.get_photo(described[0]).metadata
    kws = [k.lower() for k in meta["keywords"]]
    assert "sakura" in kws and kws.count("sakura") == 1 and "zzz-new" in kws
    assert "japan" not in kws
    assert meta["categories"] == ["Nature"] and meta["editorial"] is True
    with pytest.raises(pipeline.PipelineError):
        pipeline.bulk_edit(cfg, store, described, categories=["a", "b", "c"])


def test_batch_summary(cfg, store, photos_dir):
    batch = _approved(cfg, store, photos_dir)
    info = pipeline.batch_summary(store, batch)
    assert info["total"] == 5
    assert info["counts"]["approved"] == 3 and info["counts"]["culled"] == 2
    expected = sum(
        (photos_dir / p.orig_name).stat().st_size
        for p in store.list_photos(batch, [Status.APPROVED])
    )
    assert info["upload_bytes"] == expected > 0
    victim = store.list_photos(batch, [Status.APPROVED])[0]
    (photos_dir / victim.orig_name).unlink()
    assert pipeline.batch_summary(store, batch)["upload_bytes"] < expected


def test_deliver_batch_full_flow(cfg, store, photos_dir, monkeypatch):
    batch = _approved(cfg, store, photos_dir)
    monkeypatch.setenv("SHUTO_FTPS_USER", "u")
    monkeypatch.setenv("SHUTO_FTPS_PASSWORD", "p")
    ftp = FakeFTP()
    seen = []
    res = pipeline.deliver_batch(
        cfg,
        store,
        batch,
        confirm=True,
        progress=lambda d, t, m: seen.append((d, t)),
        connect=lambda *a, **k: ftp,
    )
    assert len(res.prepare.prepared) == 3 and len(res.upload.uploaded) == 3
    assert res.export is not None and res.export.files
    assert set(ftp.stored) == set(res.prepare.prepared)
    dones = [d for d, _ in seen]
    assert dones == sorted(dones) and dones[-1] == 7 and {t for _, t in seen} == {7}
    assert len(store.list_photos(batch, [Status.EXPORTED])) == 3


def test_deliver_without_confirm_is_refused(cfg, store, photos_dir):
    batch = _approved(cfg, store, photos_dir)
    with pytest.raises(pipeline.UploadRefused):
        pipeline.deliver_batch(cfg, store, batch, confirm=False)
    assert not store.list_photos(batch, [Status.PREPARED, Status.UPLOADED])


def test_deliver_stops_after_upload_failure(cfg, store, photos_dir, monkeypatch):
    batch = _approved(cfg, store, photos_dir)
    monkeypatch.setenv("SHUTO_FTPS_USER", "u")
    monkeypatch.setenv("SHUTO_FTPS_PASSWORD", "p")

    class Broken(FakeFTP):
        def storbinary(self, cmd, fh):
            raise OSError("disk full")

    res = pipeline.deliver_batch(cfg, store, batch, confirm=True, connect=lambda *a, **k: Broken())
    assert res.upload is not None and res.upload.failed
    assert res.export is None


def test_ftps_login_status_and_test(cfg, monkeypatch):
    status = pipeline.ftps_login_status(cfg)
    assert status == {"user_set": False, "password_set": False, "keyring_ok": False}
    with pytest.raises(pipeline.PipelineError, match="not saved"):
        pipeline.test_ftps_login(cfg)
    monkeypatch.setenv("SHUTO_FTPS_USER", "u")
    monkeypatch.setenv("SHUTO_FTPS_PASSWORD", "p")
    assert pipeline.ftps_login_status(cfg)["user_set"] is True
    ftp = FakeFTP()
    pipeline.test_ftps_login(cfg, connect=lambda *a, **k: ftp)

    def refuse(*a, **k):
        raise ftplib.error_perm("530 Login incorrect")

    with pytest.raises(pipeline.PipelineError, match="refused"):
        pipeline.test_ftps_login(cfg, connect=refuse)

    def down(*a, **k):
        raise OSError("timed out")

    with pytest.raises(pipeline.PipelineError, match="Could not reach"):
        pipeline.test_ftps_login(cfg, connect=down)


class _Keyring:
    def __init__(self):
        self.store = {}

    def set_password(self, service, name, value):
        self.store[(service, name)] = value

    def delete_password(self, service, name):
        if (service, name) not in self.store:
            raise KeyError(name)
        del self.store[(service, name)]


def test_delete_secret(monkeypatch):
    monkeypatch.setattr(secrets, "_keyring", lambda: None)
    secrets.delete_secret("X")  # no keyring: nothing to do
    kr = _Keyring()
    monkeypatch.setattr(secrets, "_keyring", lambda: kr)
    secrets.set_secret("X", "v")
    secrets.delete_secret("X")
    assert not kr.store


def test_set_secret_message_is_plain(monkeypatch):
    monkeypatch.setattr(secrets, "_keyring", lambda: None)
    with pytest.raises(RuntimeError, match="keyring add-on"):
        secrets.set_secret("X", "v")


def test_keyring_available(monkeypatch):
    monkeypatch.setattr(secrets, "_keyring", lambda: None)
    assert secrets.keyring_available() is False
    monkeypatch.setattr(secrets, "_keyring", lambda: _Keyring())
    assert isinstance(secrets.keyring_available(), bool)


def test_clearing_style_fields_really_clears(cfg, store):
    cfg = cfg.model_copy(
        update={
            "style": cfg.style.model_copy(
                update={
                    "theme": "Config theme",
                    "keywords": cfg.style.keywords.model_copy(update={"always_include": ["x"]}),
                }
            )
        }
    )
    pipeline.set_ui_setting(store, "style", {"theme": "", "tone": "", "always_include": ""})
    styled = pipeline.apply_style_overrides(cfg, store)
    assert styled.style.theme == "" and styled.style.tone == ""
    assert styled.style.keywords.always_include == []
    assert cfg.style.theme == "Config theme"  # original untouched


def test_style_fields_not_submitted_stay_unset(cfg, store):
    stored = pipeline.set_ui_setting(store, "style", {"theme": "Only theme"})
    assert stored == {"theme": "Only theme"}
    styled = pipeline.apply_style_overrides(cfg, store)
    assert styled.style.keywords.always_include == cfg.style.keywords.always_include


def test_region_focus_setting(cfg, store):
    assert pipeline.set_ui_setting(store, "style", {"region_focus": "Japan"}) == {
        "region_focus": "japan"
    }
    styled = pipeline.apply_style_overrides(cfg, store)
    assert styled.style.region_focus == "japan"
    names = {r.id for r in pipeline.load_restrictions(styled).rules}
    assert "shuto-konbini" in names
    plain = {r.id for r in pipeline.load_restrictions(cfg).rules}
    assert "shuto-konbini" not in plain and "tokyo-skytree" in plain
    with pytest.raises(pipeline.PipelineError):
        pipeline.set_ui_setting(store, "style", {"region_focus": "mars"})


def test_bedrock_profile_setting(cfg, store):
    pipeline.set_ui_setting(store, "bedrock_profile", "photos-prod")
    assert pipeline.apply_style_overrides(cfg, store).llm.bedrock.profile == "photos-prod"
    with pytest.raises(pipeline.PipelineError):
        pipeline.set_ui_setting(store, "bedrock_profile", "bad profile; rm")


def test_bedrock_profile_cleared_overrides_config(cfg, store):
    file_cfg = cfg.model_copy(
        update={
            "llm": cfg.llm.model_copy(
                update={"bedrock": cfg.llm.bedrock.model_copy(update={"profile": "from-file"})}
            )
        }
    )
    assert pipeline.apply_style_overrides(file_cfg, store).llm.bedrock.profile == "from-file"
    pipeline.set_ui_setting(store, "bedrock_profile", "")
    assert pipeline.apply_style_overrides(file_cfg, store).llm.bedrock.profile == ""
