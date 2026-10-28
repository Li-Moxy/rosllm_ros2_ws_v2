import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from rosllm_core.llm import LLMConfig, LLMError, chat_completion, read_env_value


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            body = None
        self.server.requests.append(
            {"body": body, "headers": dict(self.headers), "path": self.path}
        )
        if self.server.delay:
            time.sleep(self.server.delay)
        payload = self.server.body.encode("utf-8")
        try:
            self.send_response(self.server.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *args):
        pass


@pytest.fixture()
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    httpd.requests = []
    httpd.delay = 0.0
    httpd.status = 200
    httpd.body = json.dumps({"choices": [{"message": {"content": "hello"}}]})
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=2.0)


def config(server, **kwargs):
    data = {
        "model": "mock-model",
        "base_url": f"http://127.0.0.1:{server.server_address[1]}/v1",
        "timeout_sec": 1.0,
    }
    data.update(kwargs)
    return LLMConfig(**data)


def test_success(server):
    result = chat_completion("hi", config(server), environ={})
    assert result.ok and result.text == "hello"
    request = server.requests[0]
    assert request["path"] == "/v1/chat/completions"
    assert request["body"]["model"] == "mock-model"
    assert request["body"]["messages"][0]["content"] == "hi"
    assert "Authorization" not in request["headers"]


def test_api_key_from_argument_and_env(server):
    result = chat_completion("hi", config(server), api_key="sekret", environ={})
    assert result.ok
    assert server.requests[0]["headers"]["Authorization"] == "Bearer sekret"

    server.requests.clear()
    cfg = config(server, api_key_env="MY_KEY")
    result = chat_completion("hi", cfg, environ={"MY_KEY": "envsecret"})
    assert result.ok
    assert server.requests[0]["headers"]["Authorization"] == "Bearer envsecret"


def test_missing_key_for_remote_host_is_rejected_without_network():
    cfg = LLMConfig(model="m", base_url="https://example.com/v1", timeout_sec=0.2)
    result = chat_completion("hi", cfg, environ={})
    assert not result.ok
    assert result.error_code == LLMError.AUTH_MISSING.value
    assert "ROSLLM_API_KEY" in result.error


def test_timeout(server):
    server.delay = 0.6
    result = chat_completion("hi", config(server, timeout_sec=0.15), environ={})
    assert not result.ok
    assert result.error_code in (LLMError.TIMEOUT.value, LLMError.NETWORK.value)


def test_http_error_and_redaction(server):
    server.status = 500
    server.body = "server said sekret"
    result = chat_completion("hi", config(server), api_key="sekret", environ={})
    assert not result.ok
    assert result.error_code == LLMError.HTTP.value
    assert result.http_status == 500
    assert "sekret" not in result.error


def test_auth_hint(server):
    server.status = 401
    result = chat_completion("hi", config(server), environ={})
    assert result.error_code == LLMError.HTTP.value
    assert "API key" in result.error


@pytest.mark.parametrize(
    "body, code",
    [
        ("not json", LLMError.INVALID_RESPONSE.value),
        ("[]", LLMError.INVALID_RESPONSE.value),
        ('{"choices": []}', LLMError.INVALID_RESPONSE.value),
        ('{"choices": [{"message": {"content": ""}}]}', LLMError.EMPTY.value),
        ('{"choices": [{"message": {"content": 5}}]}', LLMError.INVALID_RESPONSE.value),
    ],
)
def test_invalid_responses(server, body, code):
    server.body = body
    result = chat_completion("hi", config(server), environ={})
    assert not result.ok and result.error_code == code


def test_oversized_response(server):
    server.body = json.dumps({"choices": [{"message": {"content": "x" * 4000}}]})
    result = chat_completion(
        "hi", config(server, max_response_bytes=1024), environ={}
    )
    assert not result.ok and result.error_code == LLMError.OVERSIZE.value


def test_empty_and_oversized_prompt(server):
    result = chat_completion("", config(server), environ={})
    assert not result.ok and result.error_code == LLMError.INPUT.value
    result = chat_completion(
        "x" * 100, config(server, max_prompt_bytes=10), environ={}
    )
    assert not result.ok and result.error_code == LLMError.INPUT.value


@pytest.mark.parametrize(
    "kwargs",
    [
        {"temperature": 9.0},
        {"timeout_sec": 0.0},
        {"base_url": "ftp://example.com"},
        {"base_url": "not a url"},
        {"api_key_env": "bad name"},
    ],
)
def test_invalid_config(kwargs):
    data = {
        "model": "m",
        "base_url": "http://127.0.0.1:1/v1",
        "temperature": 0.5,
        "timeout_sec": 1.0,
        "api_key_env": "ROSLLM_API_KEY",
    }
    data.update(kwargs)
    result = chat_completion("hi", LLMConfig(**data), environ={})
    assert not result.ok and result.error_code == LLMError.CONFIG.value


def test_read_env_value(tmp_path):
    path = tmp_path / "llm.env"
    path.write_text(
        "# comment\n"
        "export DEEPSEEK_API_KEY='quoted-value'\n"
        "OTHER=1\n",
        encoding="utf-8",
    )
    value, error = read_env_value(path, "DEEPSEEK_API_KEY")
    assert value == "quoted-value" and not error
    value, error = read_env_value(path, "MISSING")
    assert not value and "not defined" in error
    value, error = read_env_value(path, "bad name")
    assert not value and "invalid" in error


def test_read_env_value_rejects_empty_and_oversized(tmp_path):
    path = tmp_path / "llm.env"
    path.write_text("KEY=\n", encoding="utf-8")
    value, error = read_env_value(path, "KEY")
    assert not value and "empty" in error
    path.write_text("KEY=" + "x" * 9000 + "\n", encoding="utf-8")
    value, error = read_env_value(path, "KEY")
    assert not value and "too long" in error
    path.write_text("KEY=1\n", encoding="utf-8")
    value, error = read_env_value(path, "KEY", max_bytes=2)
    assert not value and "larger" in error
