"""A minimal client for a locally running Ollama server.

Privacy and safety properties enforced here (not left to configuration):

* The base URL must be loopback (localhost, 127.0.0.0/8, ::1) unless remote use is explicitly
  allowed, because prompts contain transaction text.
* Environment proxy settings are ignored (``ProxyHandler({})``): an ``HTTP_PROXY`` variable must
  never be able to route local model traffic elsewhere.
* Redirects are refused: a local server must not be able to bounce a request to another host.
* Response size is capped and every wait has a timeout.
* The model gets no tools. Its JSON reply is returned as untrusted data for the caller to
  validate; nothing is executed or evaluated.

The request shape (``/api/chat`` with ``format`` set to a JSON schema, ``stream`` false,
``temperature`` 0, and ``/api/tags`` to list models) follows Ollama's documented REST API. It has
been tested only against a fake server in this repository, not yet against a real Ollama.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Protocol
from urllib.parse import urlparse

from bank_analyzer.config import is_loopback_url
from bank_analyzer.domain.errors import BankAnalyzerError, ConfigError

DEFAULT_TIMEOUT_S = 60
MAX_RESPONSE_BYTES = 1_000_000


class LLMError(BankAnalyzerError):
    """Base class for model errors."""


class LLMUnavailable(LLMError):
    """The model server cannot be reached or the model is not installed (core features go on)."""


class LLMResponseError(LLMError):
    """The server answered, but not with usable JSON."""


class LocalLLM(Protocol):
    """What the rest of the application needs from a model runtime."""

    model: str

    def is_available(self) -> bool:
        """True if the server is reachable and the configured model is installed."""
        ...

    def chat_json(self, system: str, user: str, schema: dict[str, Any]) -> object:
        """Send a chat request constrained to ``schema`` and return the parsed JSON reply."""
        ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None  # surfaces the 3xx as an HTTPError instead of following it


class OllamaClient:
    """Talks to Ollama over loopback HTTP using only the standard library."""

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        allow_remote: bool = False,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
    ) -> None:
        if not allow_remote and not is_loopback_url(base_url):
            raise ConfigError("The model server must be on localhost; refusing to send prompts.")
        if urlparse(base_url).scheme not in {"http", "https"}:
            raise ConfigError("The model server URL must be http or https.")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._timeout = timeout_s
        self._max_bytes = max_response_bytes
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    # -- transport ----------------------------------------------------------------------------

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> bytes:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(  # noqa: S310 - scheme validated in __init__
            self.base_url + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                body: bytes = response.read(self._max_bytes + 1)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise LLMUnavailable("The model is not installed on the local server.") from exc
            if 300 <= exc.code < 400:
                raise LLMResponseError("The local server tried to redirect the request.") from exc
            raise LLMResponseError(f"The local server returned HTTP {exc.code}.") from exc
        except (urllib.error.URLError, OSError) as exc:  # refused, unreachable, timed out
            raise LLMUnavailable("The local model server could not be reached.") from exc
        if len(body) > self._max_bytes:
            raise LLMResponseError("The model response was larger than the allowed limit.")
        return body

    # -- API ----------------------------------------------------------------------------------

    def list_models(self) -> list[str]:
        """Names of installed models. Raises LLMUnavailable if the server cannot be reached."""
        body = self._request("GET", "/api/tags")
        try:
            models = json.loads(body)["models"]
            return [str(m["name"]) for m in models]
        except (ValueError, KeyError, TypeError) as exc:
            raise LLMResponseError("The model list could not be understood.") from exc

    def is_available(self) -> bool:
        try:
            installed = self.list_models()
        except LLMError:
            return False
        wanted = self.model
        return wanted in installed or f"{wanted}:latest" in installed

    def chat_json(self, system: str, user: str, schema: dict[str, Any]) -> object:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "format": schema,
            "options": {"temperature": 0},
        }
        body = self._request("POST", "/api/chat", payload)
        try:
            content = json.loads(body)["message"]["content"]
            return json.loads(content)
        except (ValueError, KeyError, TypeError) as exc:
            raise LLMResponseError("The model did not return valid JSON.") from exc
