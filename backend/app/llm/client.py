"""Provider-agnostic wrapper around the one LLM this build talks to (BUILD_SPEC Phase 7).

``settings.llm_provider`` selects the implementation; only ``"gemini"`` exists, and any other
value is a configuration error naming what is supported. Nothing above this module knows the
provider's wire format: callers get ``is_configured()`` and ``complete()`` and nothing else.

THE RULE (BUILD_SPEC Phase 7) lives above this file: the LLM never produces a number that
appears in the UI. This module only moves text; it is the callers (extract / explain / copilot)
that hand it pre-computed values and validate every answer with Pydantic.

**No user flow may block on an LLM call.** ``complete()`` therefore returns ``None`` instead of
raising, for every failure there is: no API key, an unsupported provider, a timeout, an HTTP
4xx/5xx, a body that is not JSON, no candidates, a safety block, a truncated answer. Each one is
logged with its reason first. Callers fall back to their non-LLM path on ``None``. ``timeout_s``
is a deadline for the whole call, not for each read: whatever the provider does, the caller has
its answer — or its None, and its fallback under way — within that budget.

The API key never leaves this module. It travels in the ``x-goog-api-key`` header, never in a URL
and never in a body, so it cannot reach a log line through an echoed request. Every string this
module logs additionally goes through ``_for_log()``, which removes any key it can see and trims
the body to one line — belt and braces, because a provider's error body is not ours to trust.

Gemini wire format (verified live against this project's key before this module was written; do
not re-guess it):

    POST {base}/models/{model}:generateContent      headers x-goog-api-key, Content-Type
    {"system_instruction": {"parts": [{"text": ...}]},
     "contents": [{"role": "user", "parts": [{"text": ...}]}],
     "generationConfig": {"temperature": 0, "responseMimeType": "application/json"}}
    -> candidates[0].content.parts[0].text

    GET {base}/models                               -> models[].name / supportedGenerationMethods

``responseMimeType: application/json`` (switched on by passing ``json_schema``) makes Gemini emit
bare JSON with no code fence; the fence stripping here is for the day it stops doing that. The
schema itself is deliberately NOT forwarded as ``responseSchema``: a forced schema would take away
the "no constraints" escape hatch extract.py needs for input it cannot parse, and would make the
model invent fields instead of admitting it found none. Callers describe the shape they want in
their own prompt and validate the answer with Pydantic, which is the enforcement that matters.

The model name comes from ``settings.llm_model``. A 404 for it means the key is not offered that
model: the available names are logged (from GET /models, fetched once per process) and the error
says which one to put in LLM_MODEL, because guessing model names is how a demo dies.
"""
import asyncio
import json
import logging
import re
import time
from abc import ABC, abstractmethod
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger("greencharge.llm.client")

# The providers this build can talk to (settings.llm_provider, lowercased).
SUPPORTED_PROVIDERS = ("gemini",)

# Gemini REST surface (verified live; see the module docstring).
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
API_KEY_HEADER = "x-goog-api-key"
GENERATE_METHOD = "generateContent"
MODELS_PATH = "/models"
JSON_MIME = "application/json"

# Contract: every Phase 7 call is deterministic. The LLM classifies, extracts and narrates;
# none of that wants sampling, and a demo must give the same answer twice.
TEMPERATURE = 0

# Measured worst case on the acceptance inputs was 5.4 s. This is the ceiling before we give up
# and the caller falls back, not an expected duration; endpoints stay responsive because the
# failure path returns None rather than propagating. Kept near that measurement because extract
# and explain each retry once: a driver waits this twice before the manual form appears.
DEFAULT_TIMEOUT_S = 12.0

# Connecting is not answering: with the network off (the Phase 8 airplane-mode test) this is how
# fast we stop waiting, whatever the read timeout is.
CONNECT_TIMEOUT_S = 5.0

# GET /models is a diagnostic on an already-failed call; it must not double the wait.
LIST_MODELS_TIMEOUT_S = 10.0

# How much of a provider body reaches a log line.
LOG_BODY_CHARS = 600

# GET /models explains an already-failed call, and a wrong LLM_MODEL fails the same way every
# time: the listing (or the sentence saying why it could not be fetched) is kept per endpoint so
# only the first 404 of a process pays for the extra request. Holds names, never a key.
_model_list_cache: dict[str, list[str] | str] = {}

# API keys seen by this process, so _for_log can scrub one out of any string. Never logged,
# never returned, only compared against.
_secrets: set[str] = set()

# A fenced block: ```json\n ... \n``` or ``` ... ```. See strip_code_fences.
_OPEN_FENCE_RE = re.compile(r"^```[ \t]*[A-Za-z0-9_+.-]*[ \t]*(?:\r?\n|\Z)")
_CLOSE_FENCE_RE = re.compile(r"\r?\n?[ \t]*```[ \t]*\Z")


class LlmError(RuntimeError):
    """The LLM provider failed or answered with something unusable.

    ``complete()`` never lets this escape: it logs it and returns None so the caller falls back.
    The lower-level ``LlmClient.generate()`` raises it, which is what makes the message available
    to log in the first place.
    """


class LlmConfigError(LlmError):
    """The LLM layer is switched off or misconfigured (no key, unknown provider, no model).

    Distinguished from a call failure because it will not fix itself on the next request: it is
    logged louder, and ``is_configured()`` reports it before anything tries to call out.
    """


class LlmClient(ABC):
    """One provider. Stateless configuration; each call opens its own HTTP client."""

    provider: str

    @abstractmethod
    async def generate(
        self,
        prompt: str,
        *,
        system: str | None = None,
        json_schema: dict[str, Any] | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> str:
        """Return the model's reply text, or raise LlmError. Never returns an empty string."""


class GeminiClient(LlmClient):
    """Google Gemini via the generativelanguage.googleapis.com v1beta REST API.

    ``transport`` exists for tests (httpx.MockTransport); production passes None.
    """

    provider = "gemini"

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str = GEMINI_BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key.strip()
        self._model = model.strip()
        self._base_url = base_url.rstrip("/")
        self._transport = transport
        if len(self._api_key) >= 8:
            _secrets.add(self._api_key)

    async def generate(
        self,
        prompt: str,
        *,
        system: str | None = None,
        json_schema: dict[str, Any] | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> str:
        if not prompt.strip():
            raise LlmError("The prompt is empty; not calling Gemini.")
        if timeout_s <= 0:
            raise LlmError(f"timeout_s must be positive, got {timeout_s!r}.")

        started = time.monotonic()
        try:
            # One deadline over everything the call may do: the POST, reading its body, and the
            # model listing behind a 404. httpx bounds each operation separately instead, so a
            # provider dribbling a byte every few seconds resets the read clock for as long as it
            # likes and the caller never gets to fall back.
            async with asyncio.timeout(timeout_s):
                return await self._exchange(prompt, system, json_schema, timeout_s, started)
        except TimeoutError as exc:
            raise LlmError(
                f"Gemini {self._model} did not answer within {timeout_s:g}s "
                f"(deadline reached after {time.monotonic() - started:.1f}s)."
            ) from exc

    async def list_models(self, timeout_s: float = LIST_MODELS_TIMEOUT_S) -> list[str]:
        """Model names this key may call generateContent on (no "models/" prefix).

        Raises LlmError if the listing itself fails — it is only ever called to explain another
        failure, so its own failure must not be mistaken for an answer.
        """
        try:
            async with self._client(timeout_s) as client:
                response = await client.get(MODELS_PATH)
        except httpx.HTTPError as exc:
            raise LlmError(
                f"GET {self._base_url}{MODELS_PATH} failed: "
                f"{type(exc).__name__}: {_for_log(str(exc))}"
            ) from exc
        if response.status_code != httpx.codes.OK:
            raise LlmError(
                f"GET {self._base_url}{MODELS_PATH} returned HTTP {response.status_code}. "
                f"Response body: {_for_log(response.text)}"
            )
        try:
            data = json.loads(response.text)
        except ValueError:
            raise LlmError(
                f"GET {self._base_url}{MODELS_PATH} answered with a body that is not JSON: "
                f"{_for_log(response.text)}"
            ) from None
        models = data.get("models") if isinstance(data, dict) else None
        if not isinstance(models, list):
            raise LlmError(
                f"GET {self._base_url}{MODELS_PATH} has no 'models' list: "
                f"{_for_log(response.text)}"
            )
        names: list[str] = []
        for item in models:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            methods = item.get("supportedGenerationMethods")
            if not isinstance(name, str):
                continue
            # Keep an entry whose methods field is missing or oddly shaped: this is a hint for a
            # human, and a name we cannot classify is better shown than silently dropped.
            if isinstance(methods, list) and GENERATE_METHOD not in methods:
                continue
            names.append(name.removeprefix("models/"))
        return names

    # -- internals ----------------------------------------------------------------------

    async def _exchange(
        self,
        prompt: str,
        system: str | None,
        json_schema: dict[str, Any] | None,
        timeout_s: float,
        started: float,
    ) -> str:
        """The POST and, on a 404, the diagnostic behind it. Runs inside generate's deadline."""
        path = f"{MODELS_PATH}/{self._model}:{GENERATE_METHOD}"
        body = self._request_body(prompt, system, json_schema)
        try:
            async with self._client(timeout_s) as client:
                response = await client.post(path, json=body)
        except httpx.ConnectTimeout as exc:
            # The connect budget is the smaller one (CONNECT_TIMEOUT_S): naming the whole-call
            # budget here would tell an operator debugging airplane mode to wait 12s for a
            # failure that already happened at 5s.
            raise LlmError(
                f"Gemini {self._model} could not be reached: no TCP connection within "
                f"{min(CONNECT_TIMEOUT_S, timeout_s):g}s (ConnectTimeout). The network or the "
                f"provider is unreachable."
            ) from exc
        except httpx.TimeoutException as exc:
            raise LlmError(
                f"Gemini {self._model} did not answer within {timeout_s:g}s "
                f"({type(exc).__name__})."
            ) from exc
        except httpx.HTTPError as exc:
            raise LlmError(
                f"Gemini {self._model} could not be reached: "
                f"{type(exc).__name__}: {_for_log(str(exc))}"
            ) from exc
        elapsed = time.monotonic() - started

        status = response.status_code
        raw = response.text
        if status == httpx.codes.NOT_FOUND:
            raise await self._model_not_found(raw, timeout_s)
        if status != httpx.codes.OK:
            logger.error(
                "Gemini %s returned HTTP %d after %.1fs. Response body: %s",
                self._model, status, elapsed, _for_log(raw),
            )
            raise LlmError(
                f"Gemini {self._model} returned HTTP {status}. Response body: {_for_log(raw)}"
            )

        text = _extract_text(raw, self._model)
        logger.info(
            "Gemini %s answered in %.1fs (%d chars%s).",
            self._model, elapsed, len(text), ", JSON mode" if json_schema is not None else "",
        )
        return text

    def _request_body(
        self, prompt: str, system: str | None, json_schema: dict[str, Any] | None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if system and system.strip():
            body["system_instruction"] = {"parts": [{"text": system}]}
        body["contents"] = [{"role": "user", "parts": [{"text": prompt}]}]
        generation_config: dict[str, Any] = {"temperature": TEMPERATURE}
        if json_schema is not None:
            # JSON mode. The schema is not sent (see the module docstring): it says the caller
            # wants bare JSON back, and the caller's own Pydantic model is the enforcement.
            generation_config["responseMimeType"] = JSON_MIME
        body["generationConfig"] = generation_config
        return body

    def _client(self, timeout_s: float) -> httpx.AsyncClient:
        # One short-lived client per call: Phase 7 makes a handful of calls per demo, and a
        # client must never be shared between event loops.
        return httpx.AsyncClient(
            base_url=self._base_url,
            headers={API_KEY_HEADER: self._api_key, "Content-Type": JSON_MIME},
            timeout=httpx.Timeout(timeout_s, connect=min(CONNECT_TIMEOUT_S, timeout_s)),
            transport=self._transport,
        )

    async def _model_not_found(self, raw: str, timeout_s: float) -> LlmError:
        """Turn a 404 into an error that names the models this key actually has."""
        logger.error(
            "Gemini returned HTTP 404 for model %r. Response body: %s",
            self._model, _for_log(raw),
        )
        listed = await self._models_for_this_key(timeout_s)
        if isinstance(listed, str):
            return LlmError(
                f"Gemini has no model {self._model!r} for this API key (HTTP 404), and the "
                f"available models could not be listed either: {listed} "
                f"Set LLM_MODEL in .env to a model the key offers and restart the backend."
            )
        return LlmError(
            f"Gemini has no model {self._model!r} for this API key (HTTP 404). It offers "
            f"{len(listed)} model(s) for {GENERATE_METHOD}: {', '.join(listed) or '(none)'}. "
            f"Set LLM_MODEL in .env to one of them and restart the backend."
        )

    async def _models_for_this_key(self, timeout_s: float) -> list[str] | str:
        """The model names for this endpoint, or the sentence saying why they are unknown.

        Fetched at most once per process: LLM_MODEL cannot change while the backend runs, so the
        second 404 already knows the answer and must not spend a second request on it — that
        request is what turned one slow call into two.
        """
        cached = _model_list_cache.get(self._base_url)
        if cached is not None:
            return cached
        try:
            listed: list[str] | str = await self.list_models(
                timeout_s=min(LIST_MODELS_TIMEOUT_S, timeout_s)
            )
        except LlmError as exc:
            listed = str(exc)
        else:
            logger.error(
                "Models this API key may call %s on (%d): %s",
                GENERATE_METHOD, len(listed), ", ".join(listed) or "(none)",
            )
        _model_list_cache[self._base_url] = listed
        return listed


# -- public API ---------------------------------------------------------------------------


def configuration_problem() -> str | None:
    """Why the LLM layer cannot be used, as a sentence for an operator; None when it can.

    Routers use this for the body of their 503 so the operator reads the cause instead of
    guessing why the LLM features are quiet.
    """
    provider = settings.llm_provider.strip().lower()
    if provider not in SUPPORTED_PROVIDERS:
        supported = ", ".join(repr(name) for name in SUPPORTED_PROVIDERS)
        return (
            f"LLM_PROVIDER is {settings.llm_provider!r}; this build supports {supported} only. "
            f"Set LLM_PROVIDER in .env and restart the backend."
        )
    if not settings.llm_api_key.strip():
        return (
            "LLM_API_KEY is empty. Put the Gemini API key in .env (LLM_API_KEY=...) and restart "
            "the backend. Every other GreenCharge feature works without it."
        )
    if not settings.llm_model.strip():
        return "LLM_MODEL is empty. Set it in .env (e.g. gemini-2.5-flash) and restart the backend."
    return None


def is_configured() -> bool:
    """True when a supported provider, an API key and a model name are all present.

    Callers check this first and skip the LLM cleanly (the routers answer 503) rather than
    calling out and waiting for a failure they could have predicted.
    """
    return configuration_problem() is None


def get_client() -> LlmClient:
    """The client ``settings.llm_provider`` selects. Raises LlmConfigError if unusable."""
    problem = configuration_problem()
    if problem is not None:
        raise LlmConfigError(problem)
    provider = settings.llm_provider.strip().lower()
    if provider == "gemini":
        return GeminiClient(api_key=settings.llm_api_key, model=settings.llm_model)
    # configuration_problem() already rejected every other value; this is the belt to its braces.
    supported = ", ".join(repr(name) for name in SUPPORTED_PROVIDERS)
    raise LlmConfigError(f"Unknown LLM_PROVIDER {provider!r}: this build supports {supported}.")


async def complete(
    prompt: str,
    *,
    system: str | None = None,
    json_schema: dict[str, Any] | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    client: LlmClient | None = None,
) -> str | None:
    """Ask the model for one completion. Return its text, or None on ANY failure.

    This function never raises (short of the event loop cancelling it). No key, unknown provider,
    timeout, HTTP 4xx/5xx, unparseable body, no candidates, a safety block, a truncated answer:
    all of them are logged with their cause and become None, because BUILD_SPEC Phase 7 forbids
    blocking any user flow on an LLM call succeeding.

    Args:
        prompt: the user-turn text.
        system: the system instruction — the rules the model answers under.
        json_schema: pass the shape you want back (typically ``Model.model_json_schema()``) to
            switch the request into JSON mode. The schema is not sent to the provider; it is the
            caller's Pydantic validation that enforces it. See the module docstring.
        timeout_s: give-up time for the whole request.
        client: override the provider (tests inject an httpx.MockTransport this way).

    Returns:
        The reply text, stripped of a code fence if the model added one, or None.
    """
    try:
        llm = get_client() if client is None else client
        return await llm.generate(
            prompt, system=system, json_schema=json_schema, timeout_s=timeout_s
        )
    except LlmConfigError as exc:
        logger.error("LLM layer unavailable, the caller falls back: %s", _for_log(str(exc)))
        return None
    except LlmError as exc:
        logger.warning("LLM call failed, the caller falls back: %s", _for_log(str(exc)))
        return None
    except Exception:
        # Anything unforeseen (a provider library changing its exception tree, a bug here) must
        # still not reach the caller: the LLM is the optional half of this product.
        logger.exception("Unexpected error during an LLM call, the caller falls back")
        return None


def strip_code_fences(text: str) -> str:
    """Return ``text`` without a wrapping markdown code fence, and without outer whitespace.

    ```json\\n{...}\\n``` and ```{...}``` both become {...}. JSON mode does not fence its output,
    so this only matters when a caller asks for prose, or if that ever changes.
    """
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    opening = _OPEN_FENCE_RE.match(stripped)
    inner = stripped[opening.end():] if opening else stripped[3:]
    return _CLOSE_FENCE_RE.sub("", inner).strip()


# -- module helpers -----------------------------------------------------------------------


def _extract_text(raw: str, model: str) -> str:
    """candidates[0].content.parts[*].text, or raise LlmError saying what came back instead."""
    try:
        data = json.loads(raw)
    except ValueError:
        raise LlmError(
            f"Gemini {model} answered with a body that is not JSON: {_for_log(raw)}"
        ) from None
    if not isinstance(data, dict):
        raise LlmError(f"Gemini {model} answered with a {type(data).__name__}, not an object: "
                       f"{_for_log(raw)}")

    feedback = data.get("promptFeedback")
    if isinstance(feedback, dict) and feedback.get("blockReason"):
        raise LlmError(
            f"Gemini {model} refused the prompt (blockReason {feedback['blockReason']!r})."
        )

    candidates = data.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise LlmError(f"Gemini {model} returned no candidates: {_for_log(raw)}")
    candidate = candidates[0]
    if not isinstance(candidate, dict):
        raise LlmError(f"Gemini {model} returned a malformed candidate: {_for_log(raw)}")

    content = candidate.get("content")
    parts = content.get("parts") if isinstance(content, dict) else None
    pieces: list[str] = []
    if isinstance(parts, list):
        for part in parts:
            # 2.5-series models can return reasoning parts alongside the answer; those are
            # marked thought=true and are not the reply.
            if isinstance(part, dict) and part.get("thought") is not True:
                piece = part.get("text")
                if isinstance(piece, str):
                    pieces.append(piece)
    text = strip_code_fences("".join(pieces))

    finish = candidate.get("finishReason")
    if finish is not None and finish != "STOP":
        # SAFETY, RECITATION, MAX_TOKENS: whatever text there is, is filtered or truncated.
        raise LlmError(
            f"Gemini {model} stopped early (finishReason {finish!r}, {len(text)} chars of text); "
            f"the answer cannot be trusted: {_for_log(raw)}"
        )
    if not text:
        raise LlmError(f"Gemini {model} returned a candidate with no text: {_for_log(raw)}")
    return text


def _for_log(text: str, limit: int = LOG_BODY_CHARS) -> str:
    """One-line, length-capped, key-free version of a string that is about to be logged."""
    for secret in (settings.llm_api_key.strip(), *_secrets):
        if len(secret) >= 8 and secret in text:
            text = text.replace(secret, "<redacted>")
    collapsed = " ".join(text.split())
    if len(collapsed) > limit:
        return f"{collapsed[:limit]}... ({len(collapsed)} chars)"
    return collapsed
