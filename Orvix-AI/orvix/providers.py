"""Real multi-provider AI adapters for ORVIX AI.

Every provider is reached through its legitimate public API. Keys are read
from environment variables only (see ``PROVIDERS[*].env_key``); nothing is
hardcoded and nothing is simulated. When a provider has no key configured,
calls raise :class:`ProviderNotConfigured` with the exact variable to set.

The HTTP layer is a single injectable ``transport`` callable so unit tests
can use a local test double at the HTTP boundary. Production code always
uses the real urllib transport.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

Transport = Callable[[str, str, dict, Optional[bytes], float], tuple[int, dict, bytes]]


def urllib_transport(method: str, url: str, headers: dict, body: Optional[bytes], timeout: float):
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:  # surface status + body to caller
        return exc.code, dict(exc.headers or {}), exc.read() or b""


class ProviderError(RuntimeError):
    def __init__(self, provider: str, message: str, status: Optional[int] = None, transient: bool = False):
        super().__init__(f"[{provider}] {message}")
        self.provider, self.status, self.transient = provider, status, transient


class ProviderNotConfigured(ProviderError):
    def __init__(self, provider: str, env_key: str):
        super().__init__(provider, f"not configured: set the {env_key} environment variable")
        self.env_key = env_key


@dataclass(frozen=True)
class ModelInfo:
    provider: str
    model: str
    capabilities: frozenset
    cost_per_1k_tokens: float  # rough public list-price estimate, USD (input+output blended)
    latency_rank: int  # 1 = fastest class, 3 = slowest class
    context_tokens: int = 128000


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    env_key: Optional[str]  # None = keyless local provider
    base_url: str
    kind: str  # "openai" | "anthropic" | "gemini"
    models: tuple


def _m(provider, model, caps, cost, latency, ctx=128000):
    return ModelInfo(provider, model, frozenset(caps), cost, latency, ctx)


PROVIDERS: dict[str, ProviderSpec] = {
    "openai": ProviderSpec("openai", "OPENAI_API_KEY", "https://api.openai.com/v1", "openai", (
        _m("openai", "gpt-4o", {"reasoning", "coding", "research", "document", "vision", "chat", "verification", "planning"}, 0.0075, 2, 128000),
        _m("openai", "gpt-4o-mini", {"chat", "document", "coding", "verification"}, 0.00045, 1, 128000),
    )),
    "anthropic": ProviderSpec("anthropic", "ANTHROPIC_API_KEY", "https://api.anthropic.com", "anthropic", (
        _m("anthropic", "claude-3-7-sonnet-latest", {"reasoning", "coding", "research", "document", "chat", "verification", "planning"}, 0.009, 2, 200000),
        _m("anthropic", "claude-3-5-haiku-latest", {"chat", "document", "verification"}, 0.0016, 1, 200000),
    )),
    "gemini": ProviderSpec("gemini", "GEMINI_API_KEY", "https://generativelanguage.googleapis.com/v1beta", "gemini", (
        _m("gemini", "gemini-2.0-flash", {"chat", "document", "research", "vision", "verification"}, 0.0004, 1, 1000000),
        _m("gemini", "gemini-1.5-pro", {"reasoning", "research", "document", "planning", "verification"}, 0.005, 3, 2000000),
    )),
    "deepseek": ProviderSpec("deepseek", "DEEPSEEK_API_KEY", "https://api.deepseek.com/v1", "openai", (
        _m("deepseek", "deepseek-reasoner", {"reasoning", "coding", "planning"}, 0.0022, 3, 64000),
        _m("deepseek", "deepseek-chat", {"chat", "coding", "document"}, 0.0011, 2, 64000),
    )),
    "xai": ProviderSpec("xai", "XAI_API_KEY", "https://api.x.ai/v1", "openai", (
        _m("xai", "grok-3", {"reasoning", "research", "chat", "verification"}, 0.006, 2, 131072),
        _m("xai", "grok-3-mini", {"chat", "reasoning"}, 0.0006, 1, 131072),
    )),
    "mistral": ProviderSpec("mistral", "MISTRAL_API_KEY", "https://api.mistral.ai/v1", "openai", (
        _m("mistral", "mistral-large-latest", {"reasoning", "coding", "chat", "document"}, 0.004, 2, 128000),
        _m("mistral", "mistral-small-latest", {"chat", "document"}, 0.0004, 1, 128000),
    )),
    "openrouter": ProviderSpec("openrouter", "OPENROUTER_API_KEY", "https://openrouter.ai/api/v1", "openai", (
        _m("openrouter", "meta-llama/llama-3.3-70b-instruct", {"chat", "reasoning", "coding"}, 0.001, 2, 131072),
        _m("openrouter", "mistralai/mistral-7b-instruct", {"chat"}, 0.0002, 1, 32768),
    )),
    "ollama": ProviderSpec("ollama", None, "http://localhost:11434/v1", "openai", (
        _m("ollama", "qwen2.5:3b", {"chat", "document"}, 0.0, 1, 32768),
        _m("ollama", "llama3.1:8b", {"chat", "reasoning", "coding"}, 0.0, 2, 128000),
    )),
}

ALL_MODELS: tuple = tuple(m for spec in PROVIDERS.values() for m in spec.models)


def configured_providers(env: Optional[dict] = None) -> dict[str, bool]:
    env = os.environ if env is None else env
    out = {}
    for name, spec in PROVIDERS.items():
        if spec.env_key is None:
            out[name] = True  # local, keyless (may still be offline; health check decides)
        elif name == "gemini":
            out[name] = bool(env.get("GEMINI_API_KEY") or env.get("GOOGLE_API_KEY"))
        else:
            out[name] = bool(env.get(spec.env_key))
    return out


@dataclass
class ChatResult:
    text: str
    provider: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def estimated_cost(self, info: Optional[ModelInfo] = None) -> float:
        info = info or next((m for m in ALL_MODELS if m.provider == self.provider and m.model == self.model), None)
        return (self.total_tokens / 1000.0) * (info.cost_per_1k_tokens if info else 0.0)


class ProviderClient:
    """One real API client for one provider."""

    def __init__(self, spec: ProviderSpec, api_key: Optional[str] = None,
                 transport: Optional[Transport] = None, timeout: float = 60.0, env: Optional[dict] = None):
        self.spec = spec
        env = os.environ if env is None else env
        if api_key is not None:
            self.api_key = api_key
        elif spec.name == "gemini":
            self.api_key = env.get("GEMINI_API_KEY") or env.get("GOOGLE_API_KEY")
        elif spec.env_key:
            self.api_key = env.get(spec.env_key)
        else:
            self.api_key = None
        self.transport = transport or urllib_transport
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return self.spec.env_key is None or bool(self.api_key)

    def _require(self):
        if not self.configured:
            raise ProviderNotConfigured(self.spec.name, self.spec.env_key or "")

    def _call(self, method: str, url: str, headers: dict, payload: Optional[dict]) -> dict:
        self._require()
        body = json.dumps(payload).encode() if payload is not None else None
        try:
            status, _h, raw = self.transport(method, url, headers, body, self.timeout)
        except Exception as exc:  # network failure = transient
            raise ProviderError(self.spec.name, f"network error: {exc.__class__.__name__}", transient=True) from exc
        if status >= 400:
            transient = status in (408, 409, 429) or status >= 500
            raise ProviderError(self.spec.name, f"HTTP {status}", status=status, transient=transient)
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError) as exc:
            raise ProviderError(self.spec.name, "invalid JSON response") from exc

    def chat(self, messages: list[dict], model: str, max_tokens: int = 1024) -> ChatResult:
        kind = self.spec.kind
        if kind == "openai":
            data = self._call("POST", f"{self.spec.base_url}/chat/completions",
                              {"Authorization": f"Bearer {self.api_key or ''}", "Content-Type": "application/json"},
                              {"model": model, "messages": messages, "max_tokens": max_tokens})
            text = data["choices"][0]["message"]["content"] or ""
            usage = data.get("usage") or {}
            return ChatResult(text, self.spec.name, model, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
        if kind == "anthropic":
            system = "\n".join(m["content"] for m in messages if m["role"] == "system")
            convo = [{"role": m["role"], "content": m["content"]} for m in messages if m["role"] != "system"]
            data = self._call("POST", f"{self.spec.base_url}/v1/messages",
                              {"x-api-key": self.api_key or "", "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
                              {"model": model, "max_tokens": max_tokens, "system": system or None, "messages": convo})
            text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
            usage = data.get("usage") or {}
            return ChatResult(text, self.spec.name, model, usage.get("input_tokens", 0), usage.get("output_tokens", 0))
        # gemini
        system = "\n".join(m["content"] for m in messages if m["role"] == "system")
        contents = [{"role": "model" if m["role"] == "assistant" else "user",
                     "parts": [{"text": m["content"]}]} for m in messages if m["role"] != "system"]
        payload: dict[str, Any] = {"contents": contents, "generationConfig": {"maxOutputTokens": max_tokens}}
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        data = self._call("POST", f"{self.spec.base_url}/models/{model}:generateContent?key={self.api_key}",
                          {"Content-Type": "application/json"}, payload)
        cand = (data.get("candidates") or [{}])[0]
        text = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", []))
        usage = data.get("usageMetadata") or {}
        return ChatResult(text, self.spec.name, model, usage.get("promptTokenCount", 0), usage.get("candidatesTokenCount", 0))

    def health(self) -> bool:
        """Real connectivity check against the provider's model list endpoint."""
        if not self.configured:
            return False
        try:
            if self.spec.kind == "anthropic":
                self._call("GET", f"{self.spec.base_url}/v1/models",
                           {"x-api-key": self.api_key or "", "anthropic-version": "2023-06-01"}, None)
            elif self.spec.kind == "gemini":
                self._call("GET", f"{self.spec.base_url}/models?key={self.api_key}", {}, None)
            else:
                self._call("GET", f"{self.spec.base_url}/models",
                           {"Authorization": f"Bearer {self.api_key or ''}"}, None)
            return True
        except ProviderError:
            return False


def build_clients(env: Optional[dict] = None, transport: Optional[Transport] = None) -> dict[str, ProviderClient]:
    return {name: ProviderClient(spec, transport=transport, env=env) for name, spec in PROVIDERS.items()}
