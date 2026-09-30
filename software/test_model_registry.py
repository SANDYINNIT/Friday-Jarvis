"""Headless tests for the explicit capability registry."""

from pathlib import Path

from source.server.model_registry import CODING, FAST, GENERAL, MODEL_REGISTRY, REASONING, VISION, select_model


def test_known_metadata_is_conservative_and_complete():
    models = {model.name: model for model in MODEL_REGISTRY}
    assert models["qwen3:8b"].provider == "ollama"
    assert models["qwen3:8b"].locality == "local"
    assert {GENERAL, CODING, REASONING, "TOOLS"} <= models["qwen3:8b"].capabilities
    assert models["gemma4:e2b"].known_timeout
    assert {VISION, CODING} <= models["rafw007/gemma4-e2b-claude-coder:latest"].capabilities
    assert models["gemini-3-flash-preview:latest"].locality == "cloud"


def test_selection_is_deterministic_and_capability_specific():
    assert select_model(GENERAL).name == "qwen3:8b"
    assert select_model(FAST).name == "llama3.2:3b"
    # VISION is qwen2.5vl:3b, NOT gemma4:e2b. Verified empirically on this
    # machine: shown a solid red PNG, qwen2.5vl:3b answers "Red" while
    # gemma4:e2b returns an empty answer. gemma4:e2b is not multimodal, so it
    # was demoted (priority 10, known_timeout) rather than left as the default
    # vision slot. See the registry comment and MEMORY.md 2026-09-23.
    assert select_model(VISION).name == "qwen2.5vl:3b"
    assert select_model(CODING).name == "qwen2.5-coder:7b"
    assert select_model(REASONING).name == "qwen3:8b"


def test_vision_prefers_a_model_that_can_actually_see():
    """Guard against a phantom vision slot being reintroduced.

    gemma4:e2b is registered with VISION capability but is demoted, because it
    cannot process images. This asserts the real, usable model wins even though
    the phantom one is still present in the registry.
    """
    vision_models = [m for m in MODEL_REGISTRY if m.supports(VISION)]
    assert vision_models, "there must be at least one vision candidate"
    chosen = select_model(VISION)
    assert chosen.name == "qwen2.5vl:3b"
    # the unusable one is still listed, but must never win
    demoted = [m for m in vision_models if m.name == "gemma4:e2b"]
    assert demoted, "gemma4:e2b should still be listed for transparency"
    assert demoted[0].known_timeout
    assert demoted[0].priority > chosen.priority


def test_installed_filter_and_local_preference():
    installed = ["gemini-3-flash-preview:latest", "rafw007/gemma4-e2b-claude-coder:latest"]
    assert select_model(VISION, installed=installed).name == installed[1]
    assert select_model(VISION, installed=[installed[0]]).name == installed[0]
    assert select_model(CODING, installed=["llama3.2:3b"]) is None


def test_qwen3_default_is_preserved():
    default_source = Path(__file__).parent / "source" / "server" / "profiles" / "default.py"
    assert 'interpreter.llm.model = "ollama_chat/qwen3:8b"' in default_source.read_text(encoding="utf-8")
