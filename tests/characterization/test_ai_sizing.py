"""How big a context window shuto asks Ollama for, which models fit, and which prompt is used."""

from __future__ import annotations

from types import SimpleNamespace

from . import check_json


def _seam():
    from shuto.ai import catalog as local_models
    from shuto.ai import selection
    from shuto.ai.hardware import HardwareProfile
    from shuto.ai.providers.ollama import OllamaProvider

    return {
        "effective_num_ctx": local_models.effective_num_ctx,
        "assess": local_models.assess,
        "load_catalog": local_models.load_catalog,
        "is_small_context": local_models.is_small_context,
        "SMALL": local_models.SMALL_CONTEXT_TOKENS,
        "TARGET": local_models.NUM_CTX_TARGET,
        "use_condensed_prompt": selection.use_condensed_prompt,
        "resolve_llm": selection.resolve_llm,
        "OllamaProvider": OllamaProvider,
        "Profile": HardwareProfile,
    }


def _profile(s, *, ram_gb, gpu_gb, tier):
    return s["Profile"](
        os="linux",
        arch="x86_64",
        cpu="Test CPU",
        cores=8,
        ram_gb=ram_gb,
        gpus=[],
        apple_silicon=False,
        usable_gpu_gb=gpu_gb,
        tier=tier,
        fingerprint="test-fp",
    )


def _profiles(s):
    return {
        "gpu 24 GB": _profile(s, ram_gb=32.0, gpu_gb=24.0, tier="gpu_mid"),
        "gpu 6 GB": _profile(s, ram_gb=16.0, gpu_gb=6.0, tier="gpu_low"),
        "cpu 32 GB": _profile(s, ram_gb=32.0, gpu_gb=0.0, tier="cpu"),
        "tight 8 GB": _profile(s, ram_gb=8.0, gpu_gb=0.0, tier="cpu"),
        "no profile": None,
    }


def test_num_ctx_and_fit_golden():
    s = _seam()
    local, _api = s["load_catalog"]()
    tags = [m.tag for m in local] + ["not-a-listed-model:1b", "QWEN2.5VL:7B"]
    out: dict = {
        "constants": {"small": s["SMALL"], "target": s["TARGET"]},
        "tags": [m.tag for m in local],
        "num_ctx": {},
        "assess": {},
        "is_small_context": {},
    }
    for label, profile in _profiles(s).items():
        out["num_ctx"][label] = {t: s["effective_num_ctx"](t, profile) for t in tags}
        if profile is not None:
            out["assess"][label] = {
                m.tag: [
                    (fits, mode, reason, round(secs, 3), basis)
                    for fits, mode, reason, secs, basis in [s["assess"](m, profile)]
                ][0]
                for m in local
            }
    out["is_small_context"] = {t: s["is_small_context"](t) for t in tags}
    check_json("ai_sizing.json", out)


def test_condensed_prompt_choice():
    s = _seam()
    use = s["use_condensed_prompt"]
    local, _ = s["load_catalog"]()
    tag = local[0].tag

    def llm(name="ollama", model=tag, **attrs):
        return SimpleNamespace(name=name, model=model, **attrs)

    got = {
        "num_ctx 2048": use(llm(num_ctx=2048)),
        "num_ctx 4096": use(llm(num_ctx=4096)),
        "num_ctx 4097": use(llm(num_ctx=4097)),
        "num_ctx 8192": use(llm(num_ctx=8192)),
        "num_ctx 0 falls back to the catalogue": use(llm(num_ctx=0)),
        "no num_ctx attribute": use(llm()),
        "unknown model, no attribute": use(llm(model="unknown:1b")),
        "api provider": use(llm(name="anthropic", num_ctx=2048)),
    }
    check_json("ai_condensed_prompt.json", got)


def test_ollama_default_num_ctx():
    s = _seam()
    assert s["OllamaProvider"]("some-model").num_ctx == s["TARGET"] == 8192
    assert s["OllamaProvider"]("some-model", num_ctx=2048).num_ctx == 2048


def test_resolve_llm_asks_ollama_for_the_sized_window(cfg, store):
    """The fake machine of the test fixtures has 32 GB RAM and no GPU."""
    s = _seam()
    local, _ = s["load_catalog"]()
    got = {}
    for m in local:
        llm = s["resolve_llm"](cfg, store, provider="ollama", model=m.tag)
        got[m.tag] = [llm.name, llm.num_ctx]
    assert all(v[0] == "ollama" for v in got.values())
    check_json("ai_resolve_llm_num_ctx.json", got)
