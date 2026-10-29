from __future__ import annotations

import os
import time
from typing import Any

import pytest

from bank_analyzer.ai.local_llm import (
    LLMResponseError,
    LLMUnavailable,
    OllamaClient,
)
from bank_analyzer.domain.errors import ConfigError
from fakes import FakeOllama

SCHEMA = {"type": "object"}


def client(server: FakeOllama, **kwargs: Any) -> OllamaClient:
    return OllamaClient(server.url, "llama3.2:3b", timeout_s=kwargs.pop("timeout_s", 5), **kwargs)


class TestLoopbackEnforcement:
    @pytest.mark.parametrize(
        "url",
        [
            "http://192.168.1.5:11434",
            "http://example.com",
            "http://0.0.0.0:11434",
            "ftp://127.0.0.1",
        ],
    )
    def test_non_loopback_refused(self, url: str) -> None:
        with pytest.raises(ConfigError):
            OllamaClient(url, "m")

    @pytest.mark.parametrize("url", ["http://127.0.0.1:1", "http://localhost:1", "http://[::1]:1"])
    def test_loopback_accepted(self, url: str) -> None:
        assert OllamaClient(url, "m").base_url == url

    def test_remote_needs_explicit_override(self) -> None:
        assert OllamaClient("http://10.0.0.9:11434", "m", allow_remote=True).model == "m"

    def test_trailing_slash_normalised(self) -> None:
        assert OllamaClient("http://127.0.0.1:1/", "m").base_url == "http://127.0.0.1:1"


class TestAvailability:
    def test_available_when_model_installed(self) -> None:
        with FakeOllama(models=("llama3.2:3b", "other:7b")) as server:
            assert client(server).is_available()
            assert client(server).list_models() == ["llama3.2:3b", "other:7b"]

    def test_latest_tag_matches_bare_name(self) -> None:
        with FakeOllama(models=("llama3.2:latest",)) as server:
            assert OllamaClient(server.url, "llama3.2").is_available()

    def test_not_available_when_model_missing(self) -> None:
        with FakeOllama(models=("something-else:1b",)) as server:
            assert not client(server).is_available()

    def test_not_available_when_server_down(self) -> None:
        # port 1 on loopback: nothing listens
        assert not OllamaClient("http://127.0.0.1:1", "m", timeout_s=2).is_available()
        with pytest.raises(LLMUnavailable):
            OllamaClient("http://127.0.0.1:1", "m", timeout_s=2).list_models()

    @pytest.mark.parametrize(
        "raw", [b"<html>nope</html>", b'{"models": 5}', b'{"other": []}', b"[]"]
    )
    def test_garbled_model_list_is_a_response_error(self, raw: bytes) -> None:
        with FakeOllama() as server:
            server.tags_override = raw
            with pytest.raises(LLMResponseError, match="model list"):
                client(server).list_models()
            assert not client(server).is_available()  # never raises, just "not available"


class TestChat:
    def test_request_shape(self) -> None:
        with FakeOllama(chat=lambda p: FakeOllama.wrap({"results": []})) as server:
            reply = client(server).chat_json("SYS", "USR", SCHEMA)
            payload = server.chat_requests()[0]
        assert reply == {"results": []}
        assert payload["messages"][0]["content"] == "SYS"
        assert payload["messages"][1]["content"] == "USR"
        assert payload["model"] == "llama3.2:3b"
        assert payload["stream"] is False and payload["format"] == SCHEMA
        assert payload["options"] == {"temperature": 0}
        assert [m["role"] for m in payload["messages"]] == ["system", "user"]
        assert set(payload) == {"model", "messages", "stream", "format", "options"}  # no tools

    def test_returns_parsed_json_content(self) -> None:
        with FakeOllama(chat=lambda p: FakeOllama.wrap({"results": [1, 2]})) as server:
            assert client(server).chat_json("s", "u", SCHEMA) == {"results": [1, 2]}

    @pytest.mark.parametrize(
        "content",
        ["not json at all", "", '{"truncated": ', "[1, 2"],
    )
    def test_invalid_json_content_is_a_response_error(self, content: str) -> None:
        with (
            FakeOllama(chat=lambda p: FakeOllama.wrap(content)) as server,
            pytest.raises(LLMResponseError, match="valid JSON"),
        ):
            client(server).chat_json("s", "u", SCHEMA)

    @pytest.mark.parametrize("body", [{"no": "message"}, {"message": "str"}, {"message": {}}, []])
    def test_unexpected_envelope_is_a_response_error(self, body: object) -> None:
        with FakeOllama(chat=lambda p: body) as server, pytest.raises(LLMResponseError):
            client(server).chat_json("s", "u", SCHEMA)

    def test_missing_model_is_unavailable_not_a_crash(self) -> None:
        with (
            FakeOllama(chat=lambda p: (404, b'{"error":"model not found"}', {})) as server,
            pytest.raises(LLMUnavailable, match="not installed"),
        ):
            client(server).chat_json("s", "u", SCHEMA)

    def test_server_error_is_a_response_error(self) -> None:
        with (
            FakeOllama(chat=lambda p: (500, b"boom", {})) as server,
            pytest.raises(LLMResponseError, match="HTTP 500"),
        ):
            client(server).chat_json("s", "u", SCHEMA)

    def test_oversized_response_is_refused(self) -> None:
        big = FakeOllama.wrap("x" * 5000)
        with (
            FakeOllama(chat=lambda p: big) as server,
            pytest.raises(LLMResponseError, match="larger than"),
        ):
            client(server, max_response_bytes=1000).chat_json("s", "u", SCHEMA)

    def test_slow_server_times_out_as_unavailable(self) -> None:
        def slow(payload: dict[str, Any]) -> Any:
            time.sleep(1)
            return FakeOllama.wrap({"results": []})

        with FakeOllama(chat=slow) as server, pytest.raises(LLMUnavailable):
            client(server, timeout_s=0.3).chat_json("s", "u", SCHEMA)


class TestNetworkContainment:
    def test_redirects_are_not_followed(self) -> None:
        with (
            FakeOllama() as target,
            FakeOllama(
                chat=lambda p: (302, b"", {"Location": target.url + "/api/chat"})
            ) as redirector,
        ):
            with pytest.raises(LLMResponseError, match="redirect"):
                client(redirector).chat_json("s", "u", SCHEMA)
            assert target.chat_requests() == []  # the redirect target was never contacted

    def test_environment_proxies_are_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A proxy variable must not reroute (or break) local model traffic."""
        for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY"):
            monkeypatch.setenv(name, "http://127.0.0.1:9")  # nothing listens there
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)
        with FakeOllama() as server:
            assert client(server).is_available()
            assert os.environ["HTTP_PROXY"] == "http://127.0.0.1:9"
