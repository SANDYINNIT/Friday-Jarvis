"""Small, deterministic capability registry for FRIDAY models."""

from dataclasses import dataclass

GENERAL = "GENERAL"
FAST = "FAST"
VISION = "VISION"
CODING = "CODING"
REASONING = "REASONING"
EXTERNAL = "EXTERNAL"


@dataclass(frozen=True)
class ModelInfo:
    name: str
    provider: str
    locality: str
    capabilities: frozenset
    priority: int
    risk: str
    latency: str
    known_timeout: bool = False

    def supports(self, task):
        return str(task).upper() in self.capabilities

    def available(self, installed=None):
        if installed is None:
            return True
        return _normalise_name(self.name) in {_normalise_name(name) for name in installed}


# Priority is an explicit tie-breaker, not a quality claim.
MODEL_REGISTRY = (
    ModelInfo("qwen3:8b", "ollama", "local", frozenset({GENERAL, CODING, REASONING, "TOOLS"}), 10, "medium", "medium"),
    ModelInfo("llama3.2:3b", "ollama", "local", frozenset({FAST}), 10, "low", "fast"),
    ModelInfo("qwen2.5-coder:7b", "ollama", "local", frozenset({CODING}), 10, "medium", "medium"),
    # Real local vision: gemma4:e2b is NOT multimodal (see MEMORY.md 2026-09-23)
    # and was a phantom vision slot; qwen2.5vl:3b actually describes frames.
    ModelInfo("qwen2.5vl:3b", "ollama", "local", frozenset({VISION}), 5, "low", "medium"),
    ModelInfo("gemma4:e2b", "ollama", "local", frozenset({VISION}), 10, "high", "slow", True),
    ModelInfo("rafw007/gemma4-e2b-claude-coder:latest", "ollama", "local", frozenset({VISION, CODING}), 20, "high", "slow", True),
    ModelInfo("gemini-3-flash-preview:latest", "ollama", "cloud", frozenset({VISION, EXTERNAL}), 30, "medium", "medium"),
    ModelInfo("ornith:9b", "ollama", "local", frozenset({GENERAL}), 30, "medium", "medium"),
    ModelInfo("qwen2.5:7b", "ollama", "local", frozenset({GENERAL}), 40, "medium", "medium"),
    ModelInfo("deepseek-r1:1.5b", "ollama", "local", frozenset({REASONING}), 20, "medium", "slow"),
    ModelInfo("deepseek-v3.2:cloud", "ollama", "cloud", frozenset({REASONING, EXTERNAL}), 30, "high", "slow"),
    ModelInfo("gpt-oss:120b-cloud", "ollama", "cloud", frozenset({REASONING, EXTERNAL}), 40, "high", "slow"),
    ModelInfo("qwen2.5:0.5b", "ollama", "local", frozenset({FAST}), 30, "medium", "fast"),
)
MODELS = {model.name: model for model in MODEL_REGISTRY}


def _normalise_name(name):
    value = str(name).strip()
    for prefix in ("ollama_chat/", "ollama/"):
        if value.startswith(prefix):
            value = value[len(prefix):]
    return value


def select_model(task, installed=None, prefer_local=True):
    """Select a model by capability without probing or making model calls."""
    candidates = [model for model in MODEL_REGISTRY if model.supports(task) and model.available(installed)]
    if prefer_local:
        candidates.sort(key=lambda model: (model.locality != "local", model.priority, model.name))
    else:
        candidates.sort(key=lambda model: (model.priority, model.name))
    return candidates[0] if candidates else None


def availability(name, installed=None):
    """Return True/False when inventory is supplied, otherwise None."""
    model = MODELS.get(_normalise_name(name))
    if model is None:
        return False if installed is not None else None
    return model.available(installed) if installed is not None else None
