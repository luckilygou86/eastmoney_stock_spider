"""Bounded, text-only AI transport; secrets and history remain in memory.

OpenAI-compatible proxy calls use their confirmed base URL. ChatGPT login tokens
are accepted only at the official public Responses API, never at a proxy or a
ChatGPT backend endpoint. Requests are not automatically retried or redirected.
"""
from __future__ import annotations

import ipaddress
import json
import math
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, replace
from typing import Any


OFFICIAL_BASE_URL = "https://api.openai.com/v1"
MAX_REQUEST_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_MESSAGES = 48
MAX_SSE_LINE_BYTES = 256 * 1024
MAX_FORMAT_PREFIX_BYTES = 8192


class AiError(RuntimeError):
    """Safe, user-facing transport/configuration error, without bearer secrets."""

    def __init__(self, message: str, *, http_status: int | None = None,
                 code: str | None = None, param: str | None = None,
                 request_id: str | None = None) -> None:
        super().__init__(message)
        self.http_status = http_status if isinstance(http_status, int) and not isinstance(http_status, bool) and 100 <= http_status <= 599 else None
        # The client supplies only bounded, redacted fields, never a raw body.
        self.code = code
        self.param = param
        self.request_id = request_id


class AiCancelled(AiError):
    """A cancelled result must never be appended to the current conversation."""


@dataclass(frozen=True)
class AiConfig:
    auth_mode: str = "api_key"
    base_url: str = ""
    model: str = ""
    api_key: str = field(default="", repr=False)
    protocol: str = "chat_completions"
    timeout: float = 60.0
    stream_timeout: float = 600.0
    idle_timeout: float = 120.0


def _loopback(hostname: str) -> bool:
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def normalize_base_url(value: str) -> str:
    """Accept a root or a versioned base, never URL credentials or redirects."""
    if not isinstance(value, str) or not value.strip():
        raise AiError("请填写并确认 AI 服务的接口地址。")
    value = value.strip()
    if any(ord(char) <= 32 or ord(char) == 127 for char in value) or "\\" in value:
        raise AiError("AI 接口地址包含无效字符。")
    try:
        parts = urllib.parse.urlsplit(value)
        hostname = parts.hostname or ""
        port = parts.port
    except ValueError:
        raise AiError("AI 接口地址格式不正确。") from None
    if parts.scheme.lower() not in {"http", "https"} or not hostname:
        raise AiError("AI 接口地址必须为完整的 HTTPS 地址；本机服务可使用 HTTP。")
    if parts.username is not None or parts.password is not None or "?" in value or "#" in value:
        raise AiError("AI 接口地址不能包含账号、密码、查询参数或片段。")
    if parts.scheme.lower() == "http" and not _loopback(hostname):
        raise AiError("为保护密钥，只允许本机回环地址使用 HTTP；远程服务必须使用 HTTPS。")
    path = parts.path.rstrip("/")
    if any(segment in {".", ".."} for segment in path.split("/")) or "%" in path:
        raise AiError("请使用未经转义、没有相对路径的 AI 接口基础地址。")
    if path.endswith(("/chat/completions", "/responses", "/models")):
        raise AiError("请填写基础地址，不要填写具体的聊天、响应或模型接口路径。")
    if not path.endswith("/v1"):
        path += "/v1"
    canonical_host = hostname.lower()
    if ":" in canonical_host:
        canonical_host = "[" + canonical_host + "]"
    netloc = canonical_host + (f":{port}" if port is not None else "")
    return urllib.parse.urlunsplit((parts.scheme.lower(), netloc, path, "", ""))


def _clean_identifier(value: Any, label: str, *, optional: bool = False) -> str:
    if not isinstance(value, str):
        raise AiError(f"{label}格式不正确。")
    value = value.strip()
    if not value and optional:
        return ""
    if not value or len(value) > 240 or any(ord(char) <= 32 or ord(char) == 127 for char in value):
        raise AiError(f"请填写有效的{label}；不能含空白或控制字符。")
    return value


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


def _json_loads(raw: str) -> Any:
    def reject_constant(_value):
        raise ValueError("nonfinite JSON")
    return json.loads(raw, parse_constant=reject_constant)


def _cancelled(event) -> None:
    if event is not None and event.is_set():
        raise AiCancelled("AI 请求已取消；较早请求的结果不会加入当前对话。")


class _PrefixedResponse:
    """Replay a bounded format probe without losing bytes or restarting deadlines."""

    def __init__(self, response, prefix: bytes, started: float):
        self._response = response
        self._prefix = prefix
        self.headers = response.headers
        self.status = getattr(response, "status", None)
        self.read_started = started
        self.stream_state = getattr(response, "stream_state", None)

    def read(self, size: int) -> bytes:
        if self._prefix:
            chunk, self._prefix = self._prefix[:size], self._prefix[size:]
            return chunk
        return self._response.read(size)

    def read1(self, size: int) -> bytes:
        if self._prefix:
            return self.read(size)
        return getattr(self._response, "read1", self._response.read)(size)

    def readline(self, size: int) -> bytes:
        if not self._prefix:
            return self._response.readline(size)
        newline = self._prefix.find(b"\n")
        count = min(size, newline + 1 if newline >= 0 else len(self._prefix))
        chunk, self._prefix = self._prefix[:count], self._prefix[count:]
        if chunk.endswith(b"\n") or len(chunk) >= size:
            return chunk
        return chunk + self._response.readline(size - len(chunk))


class _StreamResponse:
    """Bounded chunk reads, independent idle/total clocks, and safe progress.

    read1 consumes available network chunks, so a long SSE line cannot hide
    activity until its newline arrives. The watchdog only shuts down this
    response's socket; it never retries a request or changes credentials.
    """

    PHASES = {"waiting", "connected", "processing", "receiving", "completed"}

    def __init__(self, response, client, cancel_event, progress_callback, request_started, streaming):
        self._response = response
        self._client = client
        self._cancel_event = cancel_event
        self._callback = progress_callback
        self.headers = response.headers
        self.status = getattr(response, "status", None)
        self.read_started = time.monotonic()
        self._request_started = request_started
        self.stream_state = self
        self._total_limit = client.config.stream_timeout if streaming else client.config.timeout
        self._idle_limit = client.config.idle_timeout if streaming else client.config.timeout
        self._activity = self.read_started
        self._phase = "connected"
        self._received = self._events = self._text_chars = 0
        self._last_progress = float("-inf")
        self._last_phase = ""
        self._reason = None
        self._buffer = bytearray()
        self._lock = threading.RLock()
        self._finished = threading.Event()
        # urllib HTTPResponse -> BufferedReader -> SocketIO. No shared client
        # sockets or global timeout settings are modified.
        self._socket = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
        self._watcher = threading.Thread(target=self._watch, name="stock-ai-stream-watch", daemon=True)
        self._emit(force=True)
        self._watcher.start()

    def use_stream_limits(self):
        with self._lock:
            self._total_limit = self._client.config.stream_timeout
            self._idle_limit = self._client.config.idle_timeout

    def _deadline_reason(self, now):
        if self._reason:
            return self._reason
        if now - self.read_started >= self._total_limit:
            self._reason = "total"
        elif now - self._activity >= self._idle_limit:
            self._reason = "idle"
        return self._reason

    def failure(self, prefix):
        with self._lock:
            elapsed = max(0.0, time.monotonic() - self._request_started)
            phase = {"connected": "已连接", "processing": "生成中", "receiving": "接收回答中", "completed": "已结束"}.get(self._phase, "等待中")
            detail = (f"{prefix} 已等待 {elapsed:.0f} 秒，接收 {self._received} 字节、"
                      f"{self._events} 个事件、{self._text_chars} 字回答，阶段：{phase}。"
                      "未把部分结果当作完整回答；未自动重试。")
        request_id = self._client._request_id(self.headers)
        return AiError(self._client._error_message(detail, http_status=self.status, request_id=request_id),
                       http_status=self.status, request_id=request_id)

    def check(self):
        _cancelled(self._cancel_event)
        with self._lock:
            reason = self._deadline_reason(time.monotonic())
        if reason == "total":
            raise self.failure(f"AI 本次总等待已达到 {self._total_limit:g} 秒上限，尚未确认完整结束。")
        if reason == "idle":
            raise self.failure(f"AI 连接连续 {self._idle_limit:g} 秒没有收到新数据；可能是服务或网络停滞。")

    def _emit(self, force=False):
        if self._callback is None:
            return
        with self._lock:
            now = time.monotonic()
            if not force and self._phase == self._last_phase and now - self._last_progress < 1.0:
                return
            self._last_progress, self._last_phase = now, self._phase
            progress = {"phase": self._phase, "elapsed_seconds": max(0.0, now - self._request_started),
                        "bytes_received": self._received, "events_received": self._events,
                        "text_chars": self._text_chars}
        try:
            self._callback(progress)
        except Exception:
            # A display callback cannot change inference success or leak its
            # exception, raw model events, hidden reasoning, or bearer token.
            pass

    def event(self, kind, text_chars=0):
        with self._lock:
            self._events += 1
            if kind in {"response.output_text.delta", "response.refusal.delta"}:
                self._phase = "receiving"
                self._text_chars += text_chars
            elif kind == "response.created" or kind == "response.in_progress" or kind.startswith("response.reasoning") or kind == "response.output_item.added":
                if self._phase != "receiving":
                    self._phase = "processing"
        self._emit()

    def completed(self, text_chars):
        self.check()
        with self._lock:
            self._phase = "completed"
            self._text_chars = max(self._text_chars, text_chars)
        self._emit(force=True)

    def _watch(self):
        while not self._finished.wait(0.2):
            with self._lock:
                ended = self._deadline_reason(time.monotonic()) is not None
            cancelled = self._cancel_event is not None and self._cancel_event.is_set()
            if ended or cancelled:
                if self._socket is not None:
                    try:
                        self._socket.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                return
            self._emit()

    def finish(self):
        self._finished.set()
        self._watcher.join(timeout=1.0)
        self._callback = None

    def _chunk(self, size):
        self.check()
        with self._lock:
            now = time.monotonic()
            wait = max(0.001, min(self._total_limit - (now - self.read_started), self._idle_limit - (now - self._activity)))
        if self._socket is not None:
            self._socket.settimeout(wait)
        try:
            raw = getattr(self._response, "read1", self._response.read)(min(size, 4096))
        except (TimeoutError, socket.timeout):
            with self._lock:
                now = time.monotonic()
                self._reason = self._reason or ("total" if now - self.read_started >= self._total_limit else "idle")
            self.check()
            raise  # check always raises here
        except OSError:
            self.check()
            raise self.failure("AI 数据连接已中断。") from None
        self.check()
        if raw:
            with self._lock:
                self._received += len(raw)
                self._activity = time.monotonic()
                oversized = self._received > MAX_RESPONSE_BYTES
            if oversized:
                raise self.failure("AI 数据流超过安全大小限制。")
            self._emit()
        return raw

    def read1(self, size):
        self.check()
        if self._buffer:
            chunk = bytes(self._buffer[:size])
            del self._buffer[:size]
            return chunk
        return self._chunk(size)

    def read(self, size):
        return self.read1(size)

    def readline(self, size):
        while True:
            self.check()
            newline = self._buffer.find(b"\n")
            if newline >= 0 or len(self._buffer) >= size:
                count = min(size, newline + 1 if newline >= 0 else len(self._buffer))
                result = bytes(self._buffer[:count])
                del self._buffer[:count]
                return result
            raw = self._chunk(size - len(self._buffer))
            if not raw:
                result = bytes(self._buffer)
                self._buffer.clear()
                return result
            self._buffer.extend(raw)


class AiClient:
    def __init__(self, config: AiConfig):
        if not isinstance(config, AiConfig):
            raise AiError("AI 设置格式不正确。")
        if config.auth_mode not in {"api_key", "chatgpt"}:
            raise AiError("请选择接口密钥或官方 ChatGPT 登录方式。")
        if not isinstance(config.api_key, str) or not config.api_key.strip():
            raise AiError("请填写接口密钥，或先完成官方 ChatGPT 登录。")
        if any(ord(char) < 32 or ord(char) > 126 for char in config.api_key):
            raise AiError("密钥/登录令牌包含无效字符。")
        key = config.api_key.strip()
        if len(key) > 16384 or any(ord(char) <= 32 or ord(char) > 126 for char in key):
            raise AiError("密钥/登录令牌包含无效字符。")
        if isinstance(config.timeout, bool):
            raise AiError("AI 请求等待时间必须在 1～180 秒之间。")
        try:
            timeout = float(config.timeout)
        except (TypeError, ValueError):
            raise AiError("AI 请求等待时间格式不正确。") from None
        if not math.isfinite(timeout) or not 1 <= timeout <= 180:
            raise AiError("AI 请求等待时间必须在 1～180 秒之间。")
        timers = {}
        for name, low, high, label in (("stream_timeout", 30, 1800, "AI 总等待时间"), ("idle_timeout", 5, 300, "AI 无数据等待时间")):
            value = getattr(config, name)
            try:
                number = float(value)
            except (TypeError, ValueError, OverflowError):
                number = float("nan")
            if isinstance(value, bool) or not math.isfinite(number) or not low <= number <= high:
                raise AiError(f"{label}必须在 {low}～{high} 秒之间。")
            timers[name] = number
        base = normalize_base_url(config.base_url or (OFFICIAL_BASE_URL if config.auth_mode == "chatgpt" else ""))
        protocol = config.protocol
        if config.auth_mode == "chatgpt":
            if base != OFFICIAL_BASE_URL:
                raise AiError("ChatGPT 登录令牌只能发送到官方 https://api.openai.com/v1，不能发送给代理服务。")
            protocol = "responses"
        if protocol not in {"chat_completions", "responses"}:
            raise AiError("请选择 Chat Completions 或 Responses 协议。")
        model = _clean_identifier(config.model, "模型名称", optional=True)
        self.config = replace(config, base_url=base, api_key=key, model=model, protocol=protocol, timeout=timeout, **timers)
        self.model_labels: dict[str, str] = {}
        handlers: list[Any] = [_NoRedirect()]
        if _loopback(urllib.parse.urlsplit(base).hostname or ""):
            # Never forward local plaintext bearer credentials via environment proxies.
            handlers.append(urllib.request.ProxyHandler({}))
        self._opener = urllib.request.build_opener(*handlers)

    def _safe(self, value: Any, *, limit: int = 360) -> str:
        text = str(value or "")
        for secret in (self.config.api_key, urllib.parse.quote(self.config.api_key, safe="")):
            text = text.replace(secret, "[密钥已隐藏]")
        text = re.sub(r"(?i)(bearer\s+)[^\s\"'<>]+", r"\1[密钥已隐藏]", text)
        text = " ".join(text.split())
        return text[:limit]

    def _diagnostic(self, value: Any, *, limit: int = 120) -> str | None:
        """Keep exact ordinary codes/IDs, not arbitrary server data structures."""
        if not isinstance(value, str):
            return None
        return self._safe(value, limit=limit) or None

    def _request_id(self, headers: Any) -> str | None:
        if headers is None:
            return None
        value = headers.get("x-request-id") or headers.get("X-Request-ID")
        return self._diagnostic(value, limit=160)

    @staticmethod
    def _content_type(headers: Any) -> str:
        value = headers.get("Content-Type") or headers.get("content-type") or ""
        return value.split(";", 1)[0].strip().lower() if isinstance(value, str) else ""

    def _error_message(self, message: str, *, http_status=None, code=None,
                       param=None, request_id=None, content_type=None) -> str:
        # Worker queues retain only str(error), so safe diagnostics must also
        # be visible there rather than exclusively on exception attributes.
        fields = []
        if isinstance(http_status, int) and not isinstance(http_status, bool) and 100 <= http_status <= 599:
            fields.append(f"HTTP {http_status}")
        if content_type is not None:
            fields.append("Content-Type=" + (self._safe(content_type, limit=80) or "未提供"))
        for label, value, limit in (("错误码", code, 120), ("参数", param, 120), ("请求编号", request_id, 160)):
            safe = self._diagnostic(value, limit=limit)
            if safe:
                fields.append(label + "=" + safe)
        return message + (" 诊断：" + "；".join(fields) + "。" if fields else "")

    def _unexpected_stream(self, response, shape: str) -> AiError:
        status = getattr(response, "status", None)
        request_id = self._request_id(response.headers)
        message = (f"ChatGPT 返回了非预期响应（{shape}），未收到可确认完成的 Responses 数据流。"
                   "请检查服务可用性或网络网关；未自动重试，也未改用 API Key。")
        return AiError(self._error_message(message, http_status=status, request_id=request_id,
                                          content_type=self._content_type(response.headers)),
                       http_status=status, request_id=request_id)

    def _probe_response(self, response, cancel_event):
        """Headers are hints; identify SSE framing, never infer completion here."""
        started = getattr(response, "read_started", time.monotonic())
        length = response.headers.get("Content-Length") or response.headers.get("content-length")
        if isinstance(length, str) and length.isdigit() and int(length) > MAX_RESPONSE_BYTES:
            raise AiError("AI 返回内容超过安全大小限制。")
        prefix = b""
        head = b""
        while len(prefix) < MAX_FORMAT_PREFIX_BYTES:
            _cancelled(cancel_event)
            stream_state = getattr(response, "stream_state", None)
            if stream_state is not None:
                stream_state.check()
            elif time.monotonic() - started > self.config.timeout:
                raise AiError("AI 返回内容读取超时；未自动重试。")
            size = min(MAX_FORMAT_PREFIX_BYTES - len(prefix), MAX_RESPONSE_BYTES - len(prefix) + 1)
            raw = response.readline(size)
            _cancelled(cancel_event)
            if not raw:
                break
            prefix += raw
            if len(prefix) > MAX_RESPONSE_BYTES:
                raise AiError("AI 返回内容超过安全大小限制。")
            head = prefix.removeprefix(b"\xef\xbb\xbf").lstrip(b" \t\r\n")
            if head:
                break
        if len(prefix) >= MAX_FORMAT_PREFIX_BYTES and not head:
            raise AiError("AI 响应的空白前缀超过安全识别限制；未自动重试。")
        buffered = _PrefixedResponse(response, prefix, started)
        is_sse = head.startswith((b"data:", b"event:", b":", b"id:", b"retry:"))
        return buffered, is_sse

    def _http_error(self, exc: urllib.error.HTTPError) -> AiError:
        code = exc.code
        request_id = self._request_id(exc.headers)
        if 300 <= code < 400:
            exc.close()
            return AiError("AI 服务返回重定向；为防止密钥泄露，未跟随跳转。请核对基础地址。", http_status=code, request_id=request_id)
        labels = {
            400: "AI 服务拒绝了请求格式，请核对模型和接口协议。",
            401: "AI 身份验证失败（401）：密钥无效或登录已过期，请重新设置/登录。",
            403: "AI 服务拒绝访问（403）：请检查账户、模型权限或服务限制。",
            404: "AI 接口或模型未找到（404）：请核对基础地址、协议和模型名称。",
            408: "AI 服务等待超时（408）；未自动重试，避免重复消耗额度。",
            429: "AI 服务额度不足或请求过于频繁（429）；请检查配额后手动重试。",
        }
        detail = ""
        error_code = error_param = None
        try:
            raw = exc.read(8192).decode("utf-8", errors="replace")
            payload = _json_loads(raw)
            error = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(error, dict):
                detail = self._safe(error.get("message") or error.get("code"))
                error_code = self._diagnostic(error.get("code"))
                error_param = self._diagnostic(error.get("param"))
            elif isinstance(error, str):
                detail = self._safe(error)
            # Official direct admission can fail before Responses starts and
            # return {"detail": "..."}, without the standard API error object.
            if not detail and isinstance(payload, dict) and isinstance(payload.get("detail"), str):
                detail = self._safe(payload["detail"])
        except (OSError, ValueError, RecursionError):
            pass
        finally:
            exc.close()
        message = labels.get(code, f"AI 服务请求失败（HTTP {code}）；未自动重试。")
        return AiError(self._error_message(message + (" 服务说明：" + detail if detail else ""),
                                          http_status=code, code=error_code, param=error_param, request_id=request_id),
                       http_status=code, code=error_code, param=error_param, request_id=request_id)

    @staticmethod
    def _encode_request(payload):
        try:
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError, UnicodeError):
            raise AiError("AI 请求内容无法转换为有效 JSON。") from None
        if len(body) > MAX_REQUEST_BYTES:
            raise AiError("本轮快照和完整对话超过安全发送上限，请重新分析开启新对话；不会静默删除旧事实。")
        return body

    def _open(self, path: str, payload: dict[str, Any] | None, cancel_event):
        _cancelled(cancel_event)
        body = self._encode_request(payload) if payload is not None else None
        stream_requested = path == "/responses" and payload is not None and payload.get("stream") is True
        headers = {"Authorization": "Bearer " + self.config.api_key,
                   "Accept": "text/event-stream" if stream_requested else "application/json",
                   "User-Agent": "StockTrendWorkspace/1.0"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.config.base_url + path, data=body, headers=headers,
                                         method="POST" if body is not None else "GET")
        try:
            response = self._opener.open(request, timeout=self.config.timeout)
            if cancel_event is not None and cancel_event.is_set():
                response.close()
                _cancelled(cancel_event)
            return response
        except urllib.error.HTTPError as exc:
            if cancel_event is not None and cancel_event.is_set():
                exc.close()
                _cancelled(cancel_event)
            raise self._http_error(exc) from None
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError, ValueError) as exc:
            _cancelled(cancel_event)
            reason = getattr(exc, "reason", exc)
            raise AiError("AI 连接失败；未自动重试。" + self._safe(reason)) from None

    def _read_json(self, response, cancel_event, *, expected_stream=False) -> dict[str, Any]:
        length = response.headers.get("Content-Length")
        if length and length.isdigit() and int(length) > MAX_RESPONSE_BYTES:
            raise AiError("AI 返回内容超过安全大小限制。")
        chunks = []
        received = 0
        started = getattr(response, "read_started", time.monotonic())
        while True:
            _cancelled(cancel_event)
            stream_state = getattr(response, "stream_state", None)
            if stream_state is not None:
                stream_state.check()
            elif time.monotonic() - started > self.config.timeout:
                raise AiError("AI 返回内容读取超时；未自动重试。")
            chunk = response.read(min(65536, MAX_RESPONSE_BYTES - received + 1))
            if not chunk:
                break
            received += len(chunk)
            if received > MAX_RESPONSE_BYTES:
                raise AiError("AI 返回内容超过安全大小限制。")
            chunks.append(chunk)
        _cancelled(cancel_event)
        raw = b"".join(chunks)
        try:
            value = _json_loads(raw.decode("utf-8-sig"))
        except (UnicodeError, ValueError, RecursionError):
            if expected_stream:
                head = raw.removeprefix(b"\xef\xbb\xbf").lstrip()[:20].lower()
                shape = "空响应" if not head else "HTML 页面" if head.startswith((b"<!doctype html", b"<html")) else "无效 JSON 或无法识别的内容"
                raise self._unexpected_stream(response, shape) from None
            raise AiError("AI 返回的内容不是有效 JSON，请检查接口地址和协议。") from None
        if not isinstance(value, dict):
            if expected_stream:
                raise self._unexpected_stream(response, "非对象 JSON")
            raise AiError("AI 返回数据结构不正确。")
        if value.get("error"):
            self._response_failure(value.get("error"), http_status=getattr(response, "status", None),
                                   request_id=self._request_id(response.headers))
        if isinstance(value.get("detail"), str) and value["detail"].strip():
            self._response_failure(value["detail"], "AI 服务未开始生成回答",
                                   http_status=getattr(response, "status", None), request_id=self._request_id(response.headers))
        if expected_stream:
            kind = value.get("type")
            nested = value.get("response") if isinstance(value.get("response"), dict) else {}
            if kind in {"error", "response.failed", "response.incomplete"} or value.get("status") in {"failed", "incomplete"}:
                failure = nested.get("error") or nested.get("incomplete_details") or value.get("incomplete_details") or value
                self._response_failure(failure, "AI 返回未完成或失败的回答，未使用部分结果",
                                       http_status=getattr(response, "status", None), request_id=self._request_id(response.headers))
        return value

    def _response_failure(self, error: Any, prefix: str = "AI 生成失败", *,
                          http_status: int | None = None, request_id: str | None = None) -> None:
        if isinstance(error, dict):
            code = self._diagnostic(error.get("code"))
            param = self._diagnostic(error.get("param"))
            message = error.get("message") or code
        else:
            code, param, message = None, None, error
        if code == "subscription_sharing_usage_limit_exceeded":
            prefix = "ChatGPT 共享额度已用尽，请检查额度或稍后重试"
        elif code == "subscription_sharing_usage_unavailable":
            prefix = "ChatGPT 共享额度暂不可用，请稍后主动重试；登录信息已保留"
        elif code == "subscription_sharing_user_not_eligible":
            prefix = "当前账号、工作区或策略不允许使用 ChatGPT 计划；请检查账户权限"
        elif code in {"chatpass_v2_scope_not_authorized", "chatpass_v2_invalid_authorization_context"}:
            prefix = "ChatGPT 的授权范围或授权上下文未通过验证；请检查应用授权"
        elif code == "subscription_sharing_unsupported_capability":
            prefix = "ChatGPT 计划不支持本次请求的模型或能力；请根据参数诊断检查设置"
        raise AiError(self._error_message(prefix + ("：" + self._safe(message) if message else "。"),
                                          http_status=http_status, code=code, param=param, request_id=request_id),
                      http_status=http_status, code=code, param=param,
                      request_id=self._diagnostic(request_id, limit=160))

    def list_models(self, cancel_event=None) -> list[str]:
        """GET only: no paid inference, no model substitution or guessed model ID."""
        try:
            with self._open("/models", None, cancel_event) as response:
                payload = self._read_json(response, cancel_event)
            models = []
            labels = {}
            if isinstance(payload.get("models"), list):
                candidates = [(item.get("slug"), item.get("display_name")) for item in payload["models"] if isinstance(item, dict) and item.get("visibility") == "list"]
            elif isinstance(payload.get("data"), list):
                candidates = [(item.get("id"), item.get("display_name")) for item in payload["data"] if isinstance(item, dict)]
            else:
                raise AiError("AI 模型列表格式不正确，可手动填写服务提供的模型名称。")
            seen = set()
            for identifier, display_name in candidates:
                try:
                    identifier = _clean_identifier(identifier, "模型名称")
                except AiError:
                    continue
                if identifier not in seen:
                    seen.add(identifier)
                    models.append(identifier)
                    labels[identifier] = display_name.strip()[:240] if isinstance(display_name, str) and display_name.strip() and not any(ord(char) < 32 for char in display_name) else identifier
                if len(models) > 5000:
                    raise AiError("AI 服务返回模型数量过多，请手动填写模型名称。")
            _cancelled(cancel_event)
            self.model_labels = labels
            return models
        except AiError:
            raise
        except Exception as exc:
            _cancelled(cancel_event)
            raise AiError("读取 AI 模型列表失败：" + self._safe(exc)) from None

    def _messages(self, messages: Any, instructions: str) -> tuple[list[dict[str, str]], str]:
        if not isinstance(messages, list) or not messages or len(messages) > MAX_MESSAGES:
            raise AiError(f"对话需要 1～{MAX_MESSAGES} 条消息；过长时请清空聊天。")
        if not isinstance(instructions, str):
            raise AiError("AI 分析说明必须为文字。")
        cleaned = []
        for message in messages:
            if not isinstance(message, dict) or message.get("role") not in {"system", "developer", "user", "assistant"}:
                raise AiError("AI 对话消息角色不正确。")
            content = message.get("content")
            if not isinstance(content, str) or not content.strip():
                raise AiError("AI 对话消息必须包含非空文字。")
            role = message["role"]
            if self.config.auth_mode == "chatgpt" and role == "system":
                role = "developer"
            cleaned.append({"role": role, "content": content})
        if not any(message["role"] == "user" for message in cleaned):
            raise AiError("AI 对话需要至少一条用户问题。")
        return cleaned, instructions

    def _result(self, payload: dict[str, Any], text: str) -> dict[str, Any]:
        if not isinstance(text, str) or not text.strip():
            raise AiError("AI 没有返回可显示的文字，请核对模型能力与接口协议。")
        usage = payload.get("usage")
        return {"text": text.strip(), "usage": usage if isinstance(usage, dict) else {},
                "model": payload.get("model") or self.config.model,
                "response_id": payload.get("id") or ""}

    @staticmethod
    def _response_text(payload: dict[str, Any]) -> str:
        text = []
        for item in payload.get("output") or []:
            if not isinstance(item, dict) or item.get("type") != "message" or item.get("role", "assistant") != "assistant":
                continue
            for content in item.get("content") or []:
                if not isinstance(content, dict):
                    continue
                if content.get("type") == "output_text" and isinstance(content.get("text"), str):
                    text.append(content["text"])
                elif content.get("type") == "refusal" and isinstance(content.get("refusal"), str):
                    text.append("模型拒绝请求：" + content["refusal"])
        if not text and isinstance(payload.get("output_text"), str):
            text.append(payload["output_text"])
        return "\n".join(text)

    def _read_sse(self, response, cancel_event) -> dict[str, Any]:
        received = 0
        started = getattr(response, "read_started", time.monotonic())
        lines: list[str] = []
        event_name = ""
        deltas: list[str] = []
        created: dict[str, Any] = {}
        stream_state = getattr(response, "stream_state", None)

        def event() -> dict[str, Any] | None:
            nonlocal lines, event_name, created
            if not lines:
                event_name = ""
                return None
            raw = "\n".join(lines)
            lines = []
            name, event_name = event_name, ""
            if raw.strip() == "[DONE]":
                raise AiError("AI 数据流结束但未确认 response.completed；未把截断结果当作成功。")
            try:
                item = _json_loads(raw)
            except (ValueError, RecursionError):
                raise AiError("AI 数据流包含无效 JSON，未把部分结果当作完整回答。") from None
            if not isinstance(item, dict):
                raise AiError("AI 数据流事件格式不正确。")
            kind = item.get("type") or name
            if stream_state is not None:
                stream_state.event(kind if isinstance(kind, str) else "", len(item["delta"]) if isinstance(item.get("delta"), str) else 0)
            if kind in {"error", "response.failed", "response.incomplete"}:
                body = item.get("response") or {}
                failure = item.get("error") or body.get("error") or body.get("incomplete_details")
                # Responses error events can put code/message/param directly
                # on the event, rather than nesting them inside "error".
                if not failure:
                    failure = item if kind == "error" else item.get("message")
                self._response_failure(failure,
                                       "AI 返回未完成或失败的回答，未使用部分结果",
                                       http_status=getattr(response, "status", None), request_id=self._request_id(response.headers))
            if kind == "response.created" and isinstance(item.get("response"), dict):
                created = item["response"]
            if kind == "response.output_text.delta" and isinstance(item.get("delta"), str):
                deltas.append(item["delta"])
            elif kind == "response.refusal.delta" and isinstance(item.get("delta"), str):
                deltas.append(item["delta"])
            elif kind == "response.completed":
                body = item.get("response") or {}
                if not isinstance(body, dict) or body.get("status") not in {None, "completed"}:
                    raise AiError("AI 完成事件的状态不正确。")
                body = {**created, **body}
                text = self._response_text(body) or "".join(deltas)
                result = self._result(body, text)
                if stream_state is not None:
                    stream_state.completed(len(result["text"]))
                return result
            return None

        while True:
            _cancelled(cancel_event)
            if stream_state is not None:
                stream_state.check()
            elif time.monotonic() - started > self.config.stream_timeout:
                raise AiError("AI 数据流读取超时，未把部分结果当作完整回答。")
            raw = response.readline(MAX_SSE_LINE_BYTES + 1)
            if not raw:
                result = event()
                if result is not None:
                    _cancelled(cancel_event)
                    return result
                raise AiError("AI 数据流中断，缺少 response.completed；未把部分结果当作完整回答。")
            received += len(raw)
            if len(raw) > MAX_SSE_LINE_BYTES or received > MAX_RESPONSE_BYTES:
                raise AiError("AI 数据流超过安全大小限制。")
            try:
                line = raw.decode("utf-8-sig" if received == len(raw) else "utf-8").rstrip("\r\n")
            except UnicodeError:
                raise AiError("AI 数据流包含无效文字编码。") from None
            if not line:
                result = event()
                if result is not None:
                    _cancelled(cancel_event)
                    return result
            elif line.startswith("data:"):
                lines.append(line[5:].lstrip(" "))
            elif line.startswith("event:"):
                event_name = line[6:].strip()

    def prepare_request(self, messages: list[dict[str, Any]], instructions: str = ""):
        """Build the exact protocol body without IO or credential refresh."""
        _clean_identifier(self.config.model, "模型名称")
        cleaned, instructions = self._messages(messages, instructions)
        if self.config.protocol == "chat_completions":
            if instructions.strip():
                cleaned.insert(0, {"role": "system", "content": instructions})
            body = {"model": self.config.model, "messages": cleaned, "stream": False}
            path = "/chat/completions"
        else:
            body = {"model": self.config.model, "input": cleaned, "store": False,
                    "stream": self.config.auth_mode == "chatgpt"}
            if instructions.strip():
                body["instructions"] = instructions
            path = "/responses"
        return path, body

    def request_size(self, messages: list[dict[str, Any]], instructions: str = "") -> int:
        """Validate UTF-8 bytes (including JSON escaping), not model tokens."""
        _, body = self.prepare_request(messages, instructions)
        return len(self._encode_request(body))

    def complete(self, messages: list[dict[str, Any]], instructions: str = "", cancel_event=None, progress_callback=None) -> dict[str, Any]:
        """One inference attempt; cancellation discards results, not billing guarantees."""
        _cancelled(cancel_event)
        request_started = time.monotonic()
        path, body = self.prepare_request(messages, instructions)
        try:
            if progress_callback is not None:
                try:
                    progress_callback({"phase": "waiting", "elapsed_seconds": 0.0, "bytes_received": 0, "events_received": 0, "text_chars": 0})
                except Exception:
                    pass
            with self._open(path, body, cancel_event) as response:
                reader = response
                stream_reader = None
                try:
                    if self.config.protocol == "responses":
                        stream_reader = _StreamResponse(response, self, cancel_event, progress_callback, request_started, body["stream"])
                        reader, is_sse = self._probe_response(stream_reader, cancel_event)
                    else:
                        is_sse = self._content_type(response.headers) == "text/event-stream"
                    if is_sse:
                        if self.config.protocol != "responses":
                            raise AiError("聊天接口意外返回数据流，请选择正确的 Responses 协议。")
                        stream_reader.use_stream_limits()
                        result = self._read_sse(reader, cancel_event)
                    else:
                        payload = self._read_json(reader, cancel_event, expected_stream=self.config.auth_mode == "chatgpt")
                        if self.config.auth_mode == "chatgpt":
                            raise self._unexpected_stream(response, "JSON 数据而非事件流")
                        if self.config.protocol == "responses":
                            if payload.get("status") != "completed":
                                self._response_failure(payload.get("error") or payload.get("incomplete_details") or payload.get("status"), "AI 未返回已完成的回答",
                                                       http_status=getattr(response, "status", None), request_id=self._request_id(response.headers))
                            result = self._result(payload, self._response_text(payload))
                        else:
                            choices = payload.get("choices") or []
                            choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
                            if choice.get("finish_reason") != "stop":
                                raise AiError("AI 回答未正常完成（可能达到长度限制或被过滤），未使用截断结果。")
                            message = choice.get("message") or {}
                            text = message.get("content") or message.get("refusal") if isinstance(message, dict) else ""
                            if isinstance(text, list):
                                text = "\n".join(part.get("text", "") for part in text if isinstance(part, dict) and isinstance(part.get("text"), str))
                            result = self._result(payload, text)
                finally:
                    if stream_reader is not None:
                        stream_reader.finish()
            _cancelled(cancel_event)
            return result
        except AiError:
            raise
        except Exception as exc:
            _cancelled(cancel_event)
            raise AiError("AI 返回内容读取失败；未自动重试。" + self._safe(exc)) from None
