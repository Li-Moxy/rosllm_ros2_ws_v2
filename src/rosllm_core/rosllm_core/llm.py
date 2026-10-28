"""Minimal OpenAI-compatible chat-completions client (standard library only).

The API key is read from an environment variable at call time and is never
logged, stored in ROS parameters or written into a response.  No request is
retried automatically: a retry after a timeout can duplicate side effects and
costs.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from enum import Enum


class LLMError(str, Enum):
    CONFIG = "config_error"
    AUTH_MISSING = "auth_missing"
    NETWORK = "network_error"
    TIMEOUT = "timeout"
    HTTP = "http_error"
    INVALID_RESPONSE = "invalid_response"
    EMPTY = "empty_response"
    OVERSIZE = "oversize"
    INPUT = "invalid_input"


_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MAX_ENV_FILE_BYTES = 65536


def read_env_value(path: str | Path, name: str, *, max_bytes: int = _MAX_ENV_FILE_BYTES) -> tuple[str, str]:
    """Read one variable from a private ``KEY=value`` file without a shell.

    Returns ``(value, error)``.  Error messages contain the path and variable
    name only, never the value.  This deliberately does not source the file, so
    command substitution or arbitrary shell code cannot run.
    """
    if not isinstance(name, str) or not _ENV_NAME_RE.match(name):
        return "", "invalid environment variable name"
    source = Path(path).expanduser()
    try:
        size = source.stat().st_size
    except OSError as exc:
        return "", f"cannot read env file {source}: {exc}"
    if size > max_bytes:
        return "", f"env file {source} is larger than {max_bytes} bytes"
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as exc:
        return "", f"cannot read env file {source}: {exc}"
    for line in text.splitlines():
        item = line.strip()
        if not item or item.startswith("#"):
            continue
        if item.startswith("export "):
            item = item[7:].lstrip()
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        if key.strip() != name:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if not value:
            return "", f"{name} is empty in {source}"
        if len(value.encode("utf-8")) > 8192:
            return "", f"{name} in {source} is too long"
        return value, ""
    return "", f"{name} is not defined in {source}"


@dataclass(frozen=True)
class LLMConfig:
    model: str
    base_url: str
    temperature: float = 0.8
    timeout_sec: float = 30.0
    api_key_env: str = "ROSLLM_API_KEY"
    max_response_bytes: int = 262144
    max_prompt_bytes: int = 131072

    def validated(self) -> "LLMConfig":
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("model must be a non-empty string")
        if len(self.model) > 256:
            raise ValueError("model name is too long")
        if not isinstance(self.base_url, str):
            raise ValueError("base_url must be a string")
        parsed = urllib.parse.urlparse(self.base_url.strip())
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("base_url must be an http(s) URL with a host")
        if parsed.query or parsed.fragment:
            raise ValueError("base_url must not contain a query or fragment")
        if not 0.0 <= float(self.temperature) <= 2.0:
            raise ValueError("temperature must be within 0..2")
        if not 0.1 <= float(self.timeout_sec) <= 600.0:
            raise ValueError("timeout_sec must be within 0.1..600")
        if not isinstance(self.api_key_env, str) or not self.api_key_env:
            raise ValueError("api_key_env must be a non-empty environment variable name")
        if not self.api_key_env.replace("_", "A").isalnum():
            raise ValueError("api_key_env is not a valid environment variable name")
        if not 1024 <= int(self.max_response_bytes) <= 16 * 1024 * 1024:
            raise ValueError("max_response_bytes must be within 1 KiB..16 MiB")
        return self

    @property
    def endpoint(self) -> str:
        base = self.base_url.strip().rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return base + "/chat/completions"

    @property
    def is_loopback(self) -> bool:
        host = urllib.parse.urlparse(self.base_url).hostname or ""
        if host in ("localhost", "0.0.0.0"):
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False


@dataclass(frozen=True)
class LLMResult:
    ok: bool
    text: str = ""
    error_code: str = ""
    error: str = ""
    http_status: int | None = None
    elapsed_sec: float = 0.0

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "error_code": self.error_code,
            "error": self.error,
            "http_status": self.http_status,
            "elapsed_sec": round(self.elapsed_sec, 6),
        }


def resolve_api_key(config: LLMConfig, environ=None) -> tuple[str, str]:
    env = os.environ if environ is None else environ
    key = env.get(config.api_key_env, "")
    if not key and config.api_key_env != "OPENAI_API_KEY":
        key = env.get("OPENAI_API_KEY", "")
    key = key.strip()
    if key:
        return key, ""
    if config.is_loopback:
        return "", ""
    return "", (
        f"no API key: set the environment variable {config.api_key_env!r} "
        "(keys are never accepted as ROS parameters)"
    )


def redact(text: str, key: str) -> str:
    if not text:
        return text
    if key:
        text = text.replace(key, "***")
    return text


def chat_completion(
    prompt: str,
    config: LLMConfig,
    *,
    api_key: str | None = None,
    environ=None,
) -> LLMResult:
    started = time.monotonic()
    try:
        config = config.validated()
    except (TypeError, ValueError) as exc:
        return LLMResult(False, error_code=LLMError.CONFIG.value, error=str(exc))
    if not isinstance(prompt, str) or not prompt.strip():
        return LLMResult(False, error_code=LLMError.INPUT.value, error="prompt is empty")
    if len(prompt.encode("utf-8")) > config.max_prompt_bytes:
        return LLMResult(
            False,
            error_code=LLMError.INPUT.value,
            error=f"prompt exceeds {config.max_prompt_bytes} bytes",
        )

    if api_key is None:
        api_key, key_error = resolve_api_key(config, environ)
        if key_error:
            return LLMResult(
                False, error_code=LLMError.AUTH_MISSING.value, error=key_error
            )

    payload = {
        "model": config.model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": float(config.temperature),
        "stream": False,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "rosllm-ros2-port/0.1",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(
        config.endpoint, data=body, headers=headers, method="POST"
    )
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}) if config.is_loopback else urllib.request.ProxyHandler()
    )

    try:
        with opener.open(request, timeout=float(config.timeout_sec)) as response:
            status = response.status
            raw = response.read(config.max_response_bytes + 1)
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read(1024).decode("utf-8", "replace").strip()
        except Exception:
            detail = ""
        detail = redact(detail, api_key)
        hint = ""
        if exc.code in (401, 403):
            hint = f" (check the API key in {config.api_key_env!r})"
        return LLMResult(
            False,
            error_code=LLMError.HTTP.value,
            error=f"HTTP {exc.code}{hint}: {detail[:512]}",
            http_status=exc.code,
            elapsed_sec=time.monotonic() - started,
        )
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, (socket.timeout, TimeoutError)):
            code, message = LLMError.TIMEOUT.value, "LLM request timed out"
        else:
            code, message = LLMError.NETWORK.value, f"network error: {reason}"
        return LLMResult(
            False, error_code=code, error=redact(message, api_key),
            elapsed_sec=time.monotonic() - started,
        )
    except (socket.timeout, TimeoutError):
        return LLMResult(
            False,
            error_code=LLMError.TIMEOUT.value,
            error="LLM request timed out",
            elapsed_sec=time.monotonic() - started,
        )
    except Exception as exc:  # noqa: BLE001 - convert to a structured failure
        return LLMResult(
            False,
            error_code=LLMError.NETWORK.value,
            error=redact(f"{type(exc).__name__}: {exc}", api_key),
            elapsed_sec=time.monotonic() - started,
        )

    elapsed = time.monotonic() - started
    if len(raw) > config.max_response_bytes:
        return LLMResult(
            False,
            error_code=LLMError.OVERSIZE.value,
            error=f"response exceeds {config.max_response_bytes} bytes",
            http_status=status,
            elapsed_sec=elapsed,
        )
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return LLMResult(
            False,
            error_code=LLMError.INVALID_RESPONSE.value,
            error=f"response is not JSON: {exc}",
            http_status=status,
            elapsed_sec=elapsed,
        )
    if not isinstance(data, dict):
        return LLMResult(
            False,
            error_code=LLMError.INVALID_RESPONSE.value,
            error="response JSON is not an object",
            http_status=status,
            elapsed_sec=elapsed,
        )
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return LLMResult(
            False,
            error_code=LLMError.INVALID_RESPONSE.value,
            error="response has no choices[0] object",
            http_status=status,
            elapsed_sec=elapsed,
        )
    message = choices[0].get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str):
        return LLMResult(
            False,
            error_code=LLMError.INVALID_RESPONSE.value,
            error="response choices[0].message.content is not a string",
            http_status=status,
            elapsed_sec=elapsed,
        )
    text = content.strip()
    if not text:
        return LLMResult(
            False,
            error_code=LLMError.EMPTY.value,
            error="LLM returned an empty response",
            http_status=status,
            elapsed_sec=elapsed,
        )
    return LLMResult(
        True, text=text, http_status=status, elapsed_sec=elapsed
    )
