"""App-specific Sign in with ChatGPT (official open-source public-client flow).

Only an explicit ``sign_in`` call opens the system browser.  Credentials belong
to this application, never to Codex or a browser profile.  Windows DPAPI is
mandatory; there is intentionally no plaintext storage fallback.

Official protocol references:
https://developers.openai.com/siwc/token-sharing-open-source/sign-in
https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions
https://developers.openai.com/siwc/token-sharing-open-source/token-reference
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
from dataclasses import dataclass, field
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import math
import os
from pathlib import Path
import re
import secrets
import socket
import ssl
import threading
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPSHandler, HTTPRedirectHandler, Request, build_opener
import uuid
import webbrowser


ISSUER = "https://auth.openai.com"
AUTHORIZE_ENDPOINT = ISSUER + "/api/accounts/authorize"
TOKEN_ENDPOINT = ISSUER + "/api/accounts/oauth/token"
JWKS_ENDPOINT = ISSUER + "/.well-known/jwks.json"
DISCOVERY_ENDPOINT = ISSUER + "/.well-known/openid-configuration"
RESOURCE = "https://api.openai.com/v1"
APP_NAME = "Eastmoney Stock Spider"
APP_ID = "EastmoneyStockSpider.ChatGPTAuth.v1"
REQUESTED_SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
PLAN_SCOPES = frozenset({"resource.invoke", "chatgpt.tokens.use.direct"})
TERMINAL_REFRESH_ERRORS = frozenset({"invalid_grant", "invalid_refresh_token", "token_expired", "refresh_token_expired", "refresh_token_invalidated", "refresh_token_reused"})
MAX_RESPONSE = 256 * 1024
LOGIN_TIMEOUT = 180


class ChatGPTAuthError(RuntimeError):
    """Only fixed, human-readable messages and non-sensitive error codes."""

    def __init__(self, message: str, code: str = "auth_error", http_status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status


def _cancelled(cancel_event: Any = None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise ChatGPTAuthError("登录或请求已取消。", "cancelled")


def _remaining(deadline: float | None, maximum: float = 15) -> float:
    if deadline is None:
        return maximum
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ChatGPTAuthError("登录等待超时，请重试。", "timeout")
    return min(remaining, maximum)


def _safe_client_id(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", value) or value == "dynamic_agent_client":
        raise ChatGPTAuthError("登录未返回有效的应用注册标识，请重新登录。", "invalid_client")
    return value


def _token_text(value: Any, name: str, required: bool = True) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str) or not value or len(value) > 65536 or any(ord(char) <= 32 or ord(char) == 127 for char in value):
        raise ChatGPTAuthError("登录服务器返回的凭据格式无效，请重新登录。", "invalid_token")
    return value


def _scopes(value: Any) -> list[str]:
    if not isinstance(value, str) or len(value) > 8192:
        raise ChatGPTAuthError("登录服务器未提供有效的授权范围。", "invalid_scope")
    return sorted(set(value.split()))


def _official_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parts = urlsplit(value)
        return parts.scheme == "https" and parts.hostname == "auth.openai.com" and parts.port in (None, 443) and not parts.username and not parts.password and not parts.fragment and not parts.query and parts.path.startswith("/")
    except ValueError:
        return False


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class OfficialHTTP:
    """Small bounded HTTPS transport; credentials are form bodies, never URLs."""

    def request(self, url: str, form: dict[str, str] | None = None,
                deadline: float | None = None, allow_empty: bool = False) -> dict[str, Any]:
        if not _official_url(url):
            raise ChatGPTAuthError("拒绝访问非官方登录地址。", "unsafe_endpoint")
        headers = {"Accept": "application/json", "User-Agent": "EastmoneyStockSpider/1.0"}
        body = None
        if form is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            body = urlencode(form).encode("utf-8")
        deadline = deadline or time.monotonic() + 20
        request = Request(url, data=body, headers=headers, method="POST" if form is not None else "GET")
        opener = build_opener(_NoRedirect(), HTTPSHandler(context=ssl.create_default_context()))
        try:
            with opener.open(request, timeout=_remaining(deadline)) as response:
                if response.status != 200:
                    raise ChatGPTAuthError("官方登录服务暂时无法完成请求。", "http_error")
                chunks = []
                size = 0
                while size <= MAX_RESPONSE:
                    remaining = _remaining(deadline)
                    try:
                        response.fp.raw._sock.settimeout(remaining)
                    except AttributeError:
                        pass
                    chunk = response.read1(min(8192, MAX_RESPONSE + 1 - size))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                payload = b"".join(chunks)
        except HTTPError as exc:
            code = "http_error"
            try:
                data = json.loads(exc.read(MAX_RESPONSE + 1))
                if isinstance(data, dict) and data.get("error") in TERMINAL_REFRESH_ERRORS | {"access_denied", "invalid_client", "temporarily_unavailable"}:
                    code = data["error"]
            except (ValueError, TypeError, OSError, RecursionError):
                pass
            finally:
                exc.close()
            message = "登录凭据已失效，请重新登录。" if code in TERMINAL_REFRESH_ERRORS else {"access_denied": "本次授权未获同意。", "invalid_client": "应用注册无法使用，请重新登录。"}.get(code, "官方登录服务暂时无法完成请求，请稍后重试。")
            raise ChatGPTAuthError(message, code, exc.code) from None
        except (URLError, OSError, TimeoutError):
            raise ChatGPTAuthError("无法连接官方登录服务，请检查网络后重试。", "network_error") from None
        if len(payload) > MAX_RESPONSE:
            raise ChatGPTAuthError("登录服务器响应过大，已停止处理。", "response_too_large", 200)
        if not payload and allow_empty:
            return {}
        try:
            data = json.loads(payload)
        except (ValueError, UnicodeError, RecursionError):
            raise ChatGPTAuthError("登录服务器响应格式无效。", "invalid_response", 200) from None
        if not isinstance(data, dict):
            raise ChatGPTAuthError("登录服务器响应格式无效。", "invalid_response", 200)
        return data


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


class WindowsDPAPI:
    """Current-user DPAPI encryption, with application-specific entropy."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise ChatGPTAuthError("ChatGPT 登录安全保存目前仅支持 Windows；不会使用明文保存凭据。", "secure_storage_unavailable")
        self.crypt = ctypes.WinDLL("crypt32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        signature = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
        self.crypt.CryptProtectData.argtypes = signature
        self.crypt.CryptUnprotectData.argtypes = signature
        self.crypt.CryptProtectData.restype = wintypes.BOOL
        self.crypt.CryptUnprotectData.restype = wintypes.BOOL
        self.kernel.LocalFree.argtypes = [ctypes.c_void_p]
        self.kernel.LocalFree.restype = ctypes.c_void_p

    def _process(self, data: bytes, encrypt: bool) -> bytes:
        source = (ctypes.c_ubyte * max(1, len(data))).from_buffer_copy(data or b"\0")
        entropy_bytes = APP_ID.encode("utf-8")
        entropy = (ctypes.c_ubyte * len(entropy_bytes)).from_buffer_copy(entropy_bytes)
        in_blob = _Blob(len(data), source)
        entropy_blob = _Blob(len(entropy_bytes), entropy)
        out_blob = _Blob()
        function = self.crypt.CryptProtectData if encrypt else self.crypt.CryptUnprotectData
        try:
            if not function(ctypes.byref(in_blob), None, ctypes.byref(entropy_blob), None, None, 1, ctypes.byref(out_blob)):
                raise ChatGPTAuthError("Windows 无法安全保存或读取本应用登录，请重新登录。", "secure_storage_error")
            return ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            ctypes.memset(source, 0, len(source))
            if out_blob.pbData:
                ctypes.memset(out_blob.pbData, 0, out_blob.cbData)
                self.kernel.LocalFree(ctypes.cast(out_blob.pbData, ctypes.c_void_p))

    def protect(self, data: bytes) -> bytes:
        return self._process(data, True)

    def unprotect(self, data: bytes) -> bytes:
        return self._process(data, False)


def _owner_only(path: Path, directory: bool = False) -> None:
    """Give only the current Windows user an explicit protected full-access ACL."""
    if os.name != "nt":
        raise ChatGPTAuthError("当前系统缺少 Windows 凭据保护，不会保存登录。", "secure_storage_unavailable")
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    advapi.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    token, sid_text, descriptor = wintypes.HANDLE(), ctypes.c_void_p(), ctypes.c_void_p()
    try:
        if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
            raise OSError
        size = wintypes.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        info = ctypes.create_string_buffer(size.value)
        if not advapi.GetTokenInformation(token, 1, info, size.value, ctypes.byref(size)):
            raise OSError
        sid_pointer = ctypes.cast(info, ctypes.POINTER(ctypes.c_void_p))[0]
        if not advapi.ConvertSidToStringSidW(sid_pointer, ctypes.byref(sid_text)):
            raise OSError
        sid = ctypes.wstring_at(sid_text)
        sddl = f"D:P(A;{'OICI' if directory else ''};FA;;;{sid})"
        if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
            raise OSError
        if not advapi.SetFileSecurityW(str(path), 0x80000004, descriptor):
            raise OSError
    except (OSError, ValueError):
        raise ChatGPTAuthError("无法设置本应用登录文件的安全权限，已停止保存。", "secure_storage_error") from None
    finally:
        if token:
            kernel.CloseHandle(token)
        if sid_text:
            kernel.LocalFree(sid_text)
        if descriptor:
            kernel.LocalFree(descriptor)


def _plain_path(path: Path) -> None:
    if path.exists():
        info = path.lstat()
        if path.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ChatGPTAuthError("登录保存目录存在链接，无法保证安全，请联系维护者。", "unsafe_storage_path")


_LOCK_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.RLock] = {}


class CredentialStore:
    """One encrypted app store; atomic snapshots and cross-process token lock."""

    def __init__(self, directory: Path | None = None) -> None:
        if directory is None:
            base = os.environ.get("LOCALAPPDATA")
            if not base or not Path(base).is_absolute():
                raise ChatGPTAuthError("无法找到本用户的安全登录保存目录。", "secure_storage_unavailable")
            directory = Path(base) / "EastmoneyStockSpider" / "ChatGPTAuth"
        self.directory = Path(directory).absolute()
        self.path = self.directory / "credentials.dpapi"
        self.lock_path = self.directory / "refresh.lock"
        self._protector: WindowsDPAPI | None = None
        with _LOCK_GUARD:
            self._thread_lock = _THREAD_LOCKS.setdefault(os.path.normcase(str(self.directory)), threading.RLock())

    def _secure_dir(self) -> None:
        _plain_path(self.directory.parent)
        _plain_path(self.directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        _owner_only(self.directory, True)

    def _dpapi(self) -> WindowsDPAPI:
        if self._protector is None:
            self._protector = WindowsDPAPI()
        return self._protector

    def load(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        try:
            _plain_path(self.directory.parent)
            _plain_path(self.directory)
            _plain_path(self.path)
            if self.path.stat().st_size > 1024 * 1024:
                raise ValueError
            with self.path.open("rb") as stream:
                encrypted = stream.read(1024 * 1024 + 1)
            if len(encrypted) > 1024 * 1024:
                raise ValueError
            data = json.loads(self._dpapi().unprotect(encrypted))
            if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("registrations"), dict):
                raise ValueError
            host = data.get("host_id", "")
            identifier = uuid.UUID(host.removeprefix("urn:uuid:"))
            if not host.startswith("urn:uuid:") or identifier.version != 4 or str(identifier) != host[9:]:
                raise ValueError
            for client, record in data["registrations"].items():
                _safe_client_id(client)
                if not isinstance(record, dict) or record.get("client_id") != client or record.get("issuer") != ISSUER or not record.get("subject"):
                    raise ValueError
            return data
        except ChatGPTAuthError:
            raise
        except (OSError, ValueError, TypeError, AttributeError, UnicodeError, RecursionError):
            raise ChatGPTAuthError("本应用登录记录无法读取，请重新登录或恢复登录文件。", "invalid_storage") from None

    def save(self, state: dict[str, Any]) -> None:
        self._secure_dir()
        encrypted = self._dpapi().protect(json.dumps(state, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8"))
        temporary = self.directory / (".credentials-" + secrets.token_hex(12) + ".tmp")
        descriptor = None
        try:
            _plain_path(self.path)
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            _owner_only(temporary)
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = None
                stream.write(encrypted)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except ChatGPTAuthError:
            raise
        except OSError:
            raise ChatGPTAuthError("本应用登录无法安全保存，请检查磁盘空间和权限。", "secure_storage_error") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if temporary.exists():
                temporary.unlink()

    @contextmanager
    def locked(self, cancel_event: Any = None, deadline: float | None = None):
        if os.name != "nt":
            raise ChatGPTAuthError("Windows 安全凭据存储不可用，不会使用明文方式。", "secure_storage_unavailable")
        import msvcrt
        lock_deadline = min(deadline, time.monotonic() + 10) if deadline else time.monotonic() + 10
        acquired = False
        stream = None
        try:
            while not acquired:
                _cancelled(cancel_event)
                if time.monotonic() >= lock_deadline:
                    raise ChatGPTAuthError("另一个窗口正在更新登录，请稍后重试。", "session_busy")
                acquired = self._thread_lock.acquire(timeout=.1)
            self._secure_dir()
            _plain_path(self.lock_path)
            descriptor = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            stream = os.fdopen(descriptor, "r+b")
            _owner_only(self.lock_path)
            if self.lock_path.stat().st_size == 0:
                stream.write(b"\0")
                stream.flush()
            while True:
                _cancelled(cancel_event)
                stream.seek(0)
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= lock_deadline:
                        raise ChatGPTAuthError("另一个应用进程正在更新登录，请稍后重试。", "session_busy") from None
                    time.sleep(.05)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            if stream is not None:
                stream.close()
            if acquired:
                self._thread_lock.release()


@dataclass(repr=False)
class LoginAttempt:
    redirect_uri: str
    state: str = field(repr=False)
    nonce: str = field(repr=False)
    verifier: str = field(repr=False)
    client_id: str = ""
    deadline: float = 0

    @classmethod
    def create(cls, redirect_uri: str, client_id: str = "", deadline: float | None = None) -> "LoginAttempt":
        return cls(redirect_uri, secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_urlsafe(64), client_id, deadline or time.monotonic() + LOGIN_TIMEOUT)

    @property
    def challenge(self) -> str:
        return base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")

    def authorize_url(self, host_id: str, record: dict[str, Any] | None = None, *, enable_plan: bool = False) -> str:
        parameters = {
            "client_id": self.client_id or "dynamic_agent_client", "redirect_uri": self.redirect_uri,
            "response_type": "code", "scope": REQUESTED_SCOPES, "resource": RESOURCE,
            "ext_agent_host_id": host_id, "state": self.state, "nonce": self.nonce,
            "code_challenge": self.challenge, "code_challenge_method": "S256",
        }
        if not self.client_id:
            parameters["agent_name_hint"] = APP_NAME
        elif record:
            if record.get("id_token"):
                parameters["id_token_hint"] = record["id_token"]
            if record.get("email"):
                parameters["login_hint"] = record["email"]
        if enable_plan:
            # force_reconsent requires a separately confirmed integration rollout;
            # use the documented standard prompt only after explicit UI action.
            parameters["prompt"] = "consent"
        return AUTHORIZE_ENDPOINT + "?" + urlencode(parameters)


def validate_callback(path: str, attempt: LoginAttempt) -> tuple[str, str]:
    if time.monotonic() >= attempt.deadline:
        raise ChatGPTAuthError("登录等待超时，请重试。", "timeout")
    try:
        parts = urlsplit(path)
        if parts.scheme or parts.netloc or parts.fragment or parts.path != "/auth/callback" or len(path) > 16384:
            raise ValueError
        query = parse_qs(parts.query, keep_blank_values=True, max_num_fields=16)
        if any(len(values) != 1 for values in query.values()):
            raise ValueError
        state = query.get("state", [""])[0]
        if not state or not secrets.compare_digest(state, attempt.state):
            raise ChatGPTAuthError("登录回调不属于本次请求，已拒绝。", "state_mismatch")
        if "error" in query:
            raise ChatGPTAuthError("本次登录授权未完成，请重新登录。", "access_denied")
        code = query.get("code", [""])[0]
        if not code or len(code) > 8192 or any(ord(char) <= 32 for char in code):
            raise ValueError
        returned_client = query.get("client_id", [""])[0]
        if attempt.client_id:
            if "client_id" in query and returned_client != attempt.client_id:
                raise ChatGPTAuthError("返回的应用注册与所选账号不一致，已拒绝登录。", "client_mismatch")
            client = attempt.client_id
        else:
            client = _safe_client_id(returned_client)
        return code, client
    except ChatGPTAuthError:
        raise
    except (ValueError, TypeError):
        raise ChatGPTAuthError("登录回调格式无效，请重新登录。", "invalid_callback") from None


class _LoopbackServer(HTTPServer):
    allow_reuse_address = False

    def __init__(self, *args, **kwargs):
        self._active_connection = None
        self._connection_lock = threading.Lock()
        super().__init__(*args, **kwargs)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(.8)
        with self._connection_lock:
            self._active_connection = connection
        return connection, address

    def shutdown_request(self, request):
        with self._connection_lock:
            if self._active_connection is request:
                self._active_connection = None
        super().shutdown_request(request)

    def cancel_active(self):
        with self._connection_lock:
            connection = self._active_connection
        if connection is not None:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()

    def handle_error(self, request, client_address):
        pass  # Never print callback request diagnostics containing credentials.


class JWTVerifier:
    """Maintained PyJWT verification with fixed official JWKS and bounded cache."""

    def __init__(self, transport: OfficialHTTP) -> None:
        self.transport = transport
        self._keys: list[dict[str, Any]] = []
        self._keys_at = 0.0

    def available(self) -> None:
        try:
            import jwt
            import cryptography
        except ImportError:
            raise ChatGPTAuthError("缺少安全登录校验组件，请先安装 requirements.txt 中的 PyJWT[crypto]。", "missing_dependency") from None

    def verify(self, token: str, client_id: str, nonce: str | None,
               deadline: float | None = None, require_nonce: bool = True) -> dict[str, Any]:
        self.available()
        import jwt
        try:
            header = jwt.get_unverified_header(token)
            algorithm = header.get("alg")
            kid = header.get("kid")
            if algorithm not in {"RS256", "ES256"} or not isinstance(kid, str) or not kid or len(kid) > 200:
                raise ValueError
            for attempt in range(2):
                if not self._keys or time.monotonic() - self._keys_at > 300 or attempt:
                    response = self.transport.request(JWKS_ENDPOINT, deadline=deadline)
                    keys = response.get("keys")
                    if not isinstance(keys, list) or not 1 <= len(keys) <= 32:
                        raise ValueError
                    self._keys = keys
                    self._keys_at = time.monotonic()
                matches = [key for key in self._keys if isinstance(key, dict) and key.get("kid") == kid]
                if len(matches) == 1:
                    jwk = matches[0]
                    break
            else:
                raise ValueError
            if jwk.get("use", "sig") != "sig" or jwk.get("alg", algorithm) != algorithm or ("key_ops" in jwk and "verify" not in jwk["key_ops"]):
                raise ValueError
            if jwk.get("kty") != ("RSA" if algorithm == "RS256" else "EC"):
                raise ValueError
            key = jwt.PyJWK.from_dict(jwk, algorithm=algorithm).key
            required = ["iss", "aud", "exp", "iat", "sub"] + (["nonce"] if require_nonce else [])
            claims = jwt.decode(token, key, algorithms=[algorithm], issuer=ISSUER, audience=client_id,
                                leeway=5, options={"require": required})
            if not isinstance(claims.get("sub"), str) or not claims["sub"]:
                raise ValueError
            if "azp" in claims and claims["azp"] != client_id:
                raise ValueError
            if isinstance(claims["aud"], list) and len(claims["aud"]) > 1 and claims.get("azp") != client_id:
                raise ValueError
            if require_nonce or "nonce" in claims:
                if not isinstance(claims.get("nonce"), str) or not nonce or not secrets.compare_digest(claims["nonce"], nonce):
                    raise ValueError
            return claims
        except ChatGPTAuthError:
            raise
        except (jwt.PyJWTError, ValueError, TypeError, KeyError, AttributeError):
            raise ChatGPTAuthError("登录身份校验未通过，请重新登录。", "identity_invalid") from None


class ChatGPTAuth:
    """Local account registration and renewable session for this stock app only."""

    def __init__(self, storage_dir: Path | None = None, *, transport: OfficialHTTP | None = None) -> None:
        self.store = CredentialStore(storage_dir)
        self.transport = transport or OfficialHTTP()
        self.verifier = JWTVerifier(self.transport)
        self.last_warning = ""
        # A code can expire after dynamic registration. Keep the issued client
        # only in memory for an explicit retry; it is not yet a verified account.
        self._pending_client_id = ""
        self._blocked_clients: set[str] = set()

    @staticmethod
    def _record_status(record: dict[str, Any] | None) -> dict[str, Any]:
        record = record or {}
        signed_in = bool(record.get("id_token") or record.get("access_token") or record.get("refresh_token"))
        return {
            "signed_in": signed_in, "email": record.get("email", ""), "expires_at": record.get("expires_at"),
            "plan_enabled": signed_in and PLAN_SCOPES.issubset(set(record.get("scopes") or [])) and bool(record.get("access_token")) and not record.get("refresh_in_progress"),
            "registered": bool(record.get("client_id")), "registration_id": record.get("client_id", ""),
            "welcome_seen": bool(record.get("welcome_seen")),
            "pending_verification": bool(record.get("pending_refresh")),
            "plan_authorized": PLAN_SCOPES.issubset(set(record.get("scopes") or [])),
        }

    def status(self) -> dict[str, Any]:
        try:
            state = self.store.load() or {}
            record = (state.get("registrations") or {}).get(state.get("active_client"))
            result = self._record_status(record)
            if record and record.get("refresh_in_progress"):
                result["error"] = "登录更新尚未完成，暂不发送 AI 请求；可稍后重试或重新登录。"
            if record and record.get("client_id") in self._blocked_clients:
                result.update(signed_in=False, plan_enabled=False, requires_reauth=True, error="登录更新无法安全保存，请恢复文件权限后重新登录。")
        except ChatGPTAuthError as exc:
            result = self._record_status(None)
            result["error"] = str(exc)
        result["remote_revocation_unconfirmed"] = bool(self.last_warning)
        if self.last_warning:
            result["warning"] = self.last_warning
        return result

    def accounts(self) -> list[dict[str, Any]]:
        state = self.store.load() or {}
        return [dict(self._record_status(record), active=client == state.get("active_client")) for client, record in (state.get("registrations") or {}).items()]

    def select_account(self, registration_id: str) -> dict[str, Any]:
        client = _safe_client_id(registration_id)
        with self.store.locked():
            state = self.store.load() or {}
            if client not in (state.get("registrations") or {}):
                raise ChatGPTAuthError("本应用未保存该账号注册，请添加账号。", "unknown_account")
            state["active_client"] = client
            self.store.save(state)
        return self.status()

    def mark_welcome_seen(self) -> None:
        with self.store.locked():
            state = self.store.load() or {}
            record = (state.get("registrations") or {}).get(state.get("active_client"))
            if record and PLAN_SCOPES.issubset(set(record.get("scopes") or [])):
                record["welcome_seen"] = True
                self.store.save(state)

    def sign_in(self, cancel_event: Any = None, *, new_account: bool = False, enable_plan: bool = False) -> dict[str, Any]:
        deadline = time.monotonic() + LOGIN_TIMEOUT
        _cancelled(cancel_event)
        self.verifier.available()
        self.last_warning = ""
        if new_account:
            self._pending_client_id = ""
        with self.store.locked(cancel_event, deadline):
            state = self.store.load()
            if state is None:
                state = {"version": 1, "host_id": "urn:uuid:" + str(uuid.uuid4()), "active_client": "", "registrations": {}}
                self.store.save(state)
            record = None if new_account else state["registrations"].get(state.get("active_client"))
            request_record = record or ({"client_id": self._pending_client_id} if self._pending_client_id else None)
            code, client, attempt = self._receive_callback(state["host_id"], request_record, deadline, cancel_event, enable_plan=enable_plan)
            _cancelled(cancel_event)
            if record is None:
                self._pending_client_id = client
            try:
                response = self.transport.request(TOKEN_ENDPOINT, {
                    "grant_type": "authorization_code", "client_id": client, "code": code,
                    "code_verifier": attempt.verifier, "redirect_uri": attempt.redirect_uri, "resource": RESOURCE,
                }, deadline=deadline)
            except ChatGPTAuthError as exc:
                if exc.code == "invalid_client":
                    self._pending_client_id = ""
                raise
            claims = self.verifier.verify(_token_text(response.get("id_token"), "id_token"), client, attempt.nonce, deadline)
            if record and (claims["sub"] != record["subject"] or record["client_id"] != client):
                raise ChatGPTAuthError("返回的身份与所选账号不一致，未替换原登录。", "account_mismatch")
            # Adding a duplicate registration cannot silently replace a different
            # identity, even if the email address happens to be the same.
            existing = state["registrations"].get(client)
            if existing and existing["subject"] != claims["sub"]:
                raise ChatGPTAuthError("账号注册标识与已保存身份不一致，已拒绝。", "account_mismatch")
            fresh = self._token_record(response, client, claims, nonce=attempt.nonce, previous=existing)
            _cancelled(cancel_event)
            _remaining(deadline)
            state["registrations"][client] = fresh
            state["active_client"] = client
            self.store.save(state)
            self._pending_client_id = ""
            self._blocked_clients.discard(client)
        return self.status()

    def _receive_callback(self, host_id: str, record: dict[str, Any] | None,
                          deadline: float, cancel_event: Any, *, enable_plan: bool = False) -> tuple[str, str, LoginAttempt]:
        result: dict[str, Any] = {}
        holder: dict[str, LoginAttempt] = {}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass  # Request URLs contain one-time credentials.

            def do_GET(self):
                expected_host = f"127.0.0.1:{self.server.server_port}"
                if self.client_address[0] != "127.0.0.1" or self.headers.get("Host") != expected_host:
                    self._reply(400, "回调地址不正确。")
                    return
                try:
                    request_path = urlsplit(self.path).path
                except ValueError:
                    self._reply(400, "回调地址不正确。")
                    return
                if request_path != "/auth/callback":
                    self._reply(404, "没有该页面。")
                    return
                if result:
                    self._reply(409, "本次回调已处理。")
                    return
                try:
                    result["credentials"] = validate_callback(self.path, holder["attempt"])
                    self._reply(200, "授权回调已收到，请返回股票工具完成登录。")
                except ChatGPTAuthError as exc:
                    # A random local request must not be able to cancel the user's
                    # real pending authorization by presenting a wrong state.
                    if exc.code not in {"state_mismatch", "invalid_callback"}:
                        result["error"] = exc
                    self._reply(400, "无法校验该登录回调，请返回应用查看提示。")

            def _reply(self, status: int, text: str):
                body = text.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Security-Policy", "default-src 'none'")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    pass

        try:
            server = _LoopbackServer(("127.0.0.1", 0), Handler)
        except OSError:
            raise ChatGPTAuthError("无法创建本机登录回调端口，请检查防火墙或安全软件。", "callback_unavailable") from None
        with server:
            server.timeout = .2
            redirect = f"http://127.0.0.1:{server.server_port}/auth/callback"
            attempt = LoginAttempt.create(redirect, record["client_id"] if record else "", deadline)
            holder["attempt"] = attempt
            _cancelled(cancel_event)
            watchdog_stop = threading.Event()

            def watchdog():
                while not watchdog_stop.wait(.05):
                    if time.monotonic() >= deadline or (cancel_event is not None and cancel_event.is_set()):
                        server.cancel_active()
                        return

            watcher = threading.Thread(target=watchdog, daemon=True)
            watcher.start()
            try:
                try:
                    opened = webbrowser.open(attempt.authorize_url(host_id, record, enable_plan=enable_plan), new=1)
                except Exception:
                    raise ChatGPTAuthError("无法打开系统浏览器，请检查默认浏览器设置。", "browser_unavailable") from None
                if not opened:
                    raise ChatGPTAuthError("无法打开系统浏览器，请检查默认浏览器设置。", "browser_unavailable")
                while not result:
                    _cancelled(cancel_event)
                    _remaining(deadline)
                    server.handle_request()
                if "error" in result:
                    raise result["error"]
                code, client = result["credentials"]
                return code, client, attempt
            finally:
                watchdog_stop.set()
                server.cancel_active()
                watcher.join(timeout=.2)

    def _token_record(self, response: dict[str, Any], client: str, claims: dict[str, Any],
                      *, nonce: str, previous: dict[str, Any] | None = None, refresh: bool = False, received_at: float | None = None) -> dict[str, Any]:
        granted = _scopes(response.get("scope"))
        if "openid" not in granted:
            raise ChatGPTAuthError("登录服务器未确认身份授权范围。", "invalid_scope")
        token_type = response.get("token_type")
        has_access = bool(response.get("access_token"))
        plan_enabled = PLAN_SCOPES.issubset(set(granted))
        if (has_access or refresh or plan_enabled) and str(token_type).lower() != "bearer":
            raise ChatGPTAuthError("登录服务器未返回可用的 Bearer 凭据。", "invalid_token")
        lifetime = response.get("expires_in") if has_access or refresh or plan_enabled else max(1, claims["exp"] - time.time())
        if isinstance(lifetime, bool) or not isinstance(lifetime, (int, float)) or not math.isfinite(lifetime) or lifetime <= 0 or ((has_access or refresh or plan_enabled) and lifetime > 3600):
            raise ChatGPTAuthError("登录服务器返回的凭据有效期无效。", "invalid_token")
        access = _token_text(response.get("access_token"), "access_token", required=plan_enabled or refresh)
        renewable = _token_text(response.get("refresh_token"), "refresh_token", required="offline_access" in granted or refresh)
        saved_at = time.time() if received_at is None else received_at
        earliest = response.get("earliest_refresh_at")
        if isinstance(earliest, str):
            try:
                from datetime import datetime
                stamp = datetime.fromisoformat(earliest.replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    raise ValueError
                earliest = stamp.timestamp()
            except ValueError:
                raise ChatGPTAuthError("凭据刷新时间格式无效。", "invalid_token") from None
        if earliest is not None and (isinstance(earliest, bool) or not isinstance(earliest, (float, int)) or not math.isfinite(earliest)):
            raise ChatGPTAuthError("凭据刷新时间格式无效。", "invalid_token")
        result = dict(previous or {})
        result.update({
            "issuer": ISSUER, "subject": claims["sub"], "client_id": client,
            "email": claims.get("email") if isinstance(claims.get("email"), str) else (previous or {}).get("email", ""),
            "id_token": _token_text(response.get("id_token"), "id_token", required=not refresh) if response.get("id_token") else (previous or {}).get("id_token", ""),
            "access_token": access, "refresh_token": renewable, "token_type": "Bearer", "scopes": granted,
            "expires_at": saved_at + lifetime, "saved_at": saved_at, "nonce": nonce,
            "earliest_refresh_at": earliest, "welcome_seen": bool((previous or {}).get("welcome_seen")),
        })
        result.pop("refresh_in_progress", None)
        result.pop("pending_refresh", None)
        return result

    def _save_session_state(self, state: dict[str, Any], client: str) -> None:
        try:
            self.store.save(state)
        except (ChatGPTAuthError, OSError, ValueError, TypeError):
            self._blocked_clients.add(client)
            raise ChatGPTAuthError("登录更新无法安全保存，请恢复文件权限后重新登录。", "session_storage_failed") from None

    def _finish_refresh(self, state: dict[str, Any], record: dict[str, Any], response: dict[str, Any],
                        received_at: float, cancel_event: Any) -> str:
        client = record["client_id"]
        try:
            claims = {"sub": record["subject"], "email": record.get("email", "")}
            # Validate the complete rotating token snapshot before any potentially
            # unavailable JWKS lookup. It remains inactive until identity verifies.
            fresh = self._token_record(response, client, claims, nonce=record.get("nonce", ""), previous=record, refresh=True, received_at=received_at)
            if response.get("id_token"):
                claims = self.verifier.verify(_token_text(response["id_token"], "id_token"), client, record.get("nonce"), require_nonce=False)
                if claims["sub"] != record["subject"]:
                    raise ChatGPTAuthError("刷新返回的账号身份不一致，请重新登录。", "account_mismatch")
                fresh["email"] = claims.get("email") if isinstance(claims.get("email"), str) else record.get("email", "")
        except ChatGPTAuthError as exc:
            if exc.code in {"network_error", "http_error", "temporarily_unavailable", "timeout", "missing_dependency", "invalid_response", "response_too_large"}:
                # Preserve newly rotated credentials encrypted, but do not activate
                # them or reuse the spent old refresh token while JWKS is down.
                record["pending_refresh"] = {"response": response, "received_at": received_at}
                record["refresh_in_progress"] = True
            else:
                self._clear_tokens(record)
            self._save_session_state(state, client)
            raise
        state["registrations"][client] = fresh
        self._save_session_state(state, client)
        _cancelled(cancel_event)
        if not PLAN_SCOPES.issubset(set(fresh["scopes"])):
            raise ChatGPTAuthError("ChatGPT 计划授权已不可用，请重新登录确认权限。", "plan_not_enabled")
        if fresh["expires_at"] <= time.time():
            raise ChatGPTAuthError("登录校验等待期间凭据已过期，请重试以刷新。", "access_expired")
        return fresh["access_token"]

    def access_token(self, cancel_event: Any = None) -> str:
        _cancelled(cancel_event)
        with self.store.locked(cancel_event):
            state = self.store.load() or {}
            record = (state.get("registrations") or {}).get(state.get("active_client"))
            if not record:
                raise ChatGPTAuthError("请先使用 Continue with ChatGPT 登录。", "not_signed_in")
            client = record["client_id"]
            if client in self._blocked_clients:
                raise ChatGPTAuthError("登录更新无法安全保存，请恢复文件权限后重新登录。", "session_storage_failed")
            if record.get("pending_refresh"):
                pending = record["pending_refresh"]
                return self._finish_refresh(state, record, pending["response"], pending["received_at"], cancel_event)
            if record.get("refresh_in_progress"):
                raise ChatGPTAuthError("上次登录更新未完成，为避免重用已失效凭据，请重新登录。", "refresh_incomplete")
            if not PLAN_SCOPES.issubset(set(record.get("scopes") or [])):
                raise ChatGPTAuthError("该登录尚未授权使用 ChatGPT 计划额度，请重新登录并确认授权。", "plan_not_enabled")
            if not record.get("access_token"):
                raise ChatGPTAuthError("请先使用 Continue with ChatGPT 登录。", "not_signed_in")
            expiration = record.get("expires_at", 0)
            if not isinstance(expiration, (int, float)) or isinstance(expiration, bool) or not math.isfinite(expiration):
                raise ChatGPTAuthError("登录有效期无法校验，请重新登录。", "invalid_storage")
            earliest = record.get("earliest_refresh_at")
            should_refresh = expiration <= time.time() + 60 and (earliest is None or time.time() >= earliest)
            if not should_refresh and expiration > time.time():
                return _token_text(record["access_token"], "access_token")
            if not record.get("refresh_token"):
                raise ChatGPTAuthError("登录已过期，请重新登录。", "not_signed_in")
            if earliest is not None and time.time() < earliest:
                raise ChatGPTAuthError("登录凭据尚不能刷新，请稍后重试。", "refresh_not_ready")
            # Persist a durable intention before touching a rotating credential.
            # A crash or failed final save can therefore never silently reuse it.
            record["refresh_in_progress"] = True
            self._save_session_state(state, client)
            try:
                response = self.transport.request(TOKEN_ENDPOINT, {
                    "grant_type": "refresh_token", "client_id": record["client_id"],
                    "refresh_token": record["refresh_token"], "resource": RESOURCE,
                })
            except ChatGPTAuthError as exc:
                if exc.code in TERMINAL_REFRESH_ERRORS | {"invalid_client", "access_denied"} or (exc.http_status == 200 and exc.code in {"invalid_response", "response_too_large"}):
                    self._clear_tokens(record)
                else:
                    record.pop("refresh_in_progress", None)
                self._save_session_state(state, client)
                raise
            return self._finish_refresh(state, record, response, time.time(), cancel_event)

    @staticmethod
    def _clear_tokens(record: dict[str, Any]) -> None:
        for field_name in ("access_token", "refresh_token", "id_token", "nonce", "scopes", "expires_at", "saved_at", "earliest_refresh_at", "token_type", "refresh_in_progress", "pending_refresh"):
            record.pop(field_name, None)

    def sign_out(self) -> None:
        self.last_warning = ""
        self._pending_client_id = ""
        with self.store.locked():
            state = self.store.load() or {}
            record = (state.get("registrations") or {}).get(state.get("active_client"))
            if not record:
                return
            pending_response = (record.get("pending_refresh") or {}).get("response") or {}
            renewable = pending_response.get("refresh_token") or record.get("refresh_token")
            latest_unknown = bool(record.get("refresh_in_progress") and not record.get("pending_refresh"))
            if renewable:
                confirmed = False
                try:
                    discovery = self.transport.request(DISCOVERY_ENDPOINT)
                    endpoint = discovery.get("revocation_endpoint")
                    if discovery.get("issuer") != ISSUER or not _official_url(endpoint):
                        raise ChatGPTAuthError("官方退出地址无法校验。", "unsafe_endpoint")
                    for attempt in range(2):
                        try:
                            self.transport.request(endpoint, {"token": renewable, "token_type_hint": "refresh_token", "client_id": record["client_id"]}, allow_empty=True)
                            confirmed = True
                            break
                        except ChatGPTAuthError as exc:
                            if exc.code not in {"network_error", "http_error"} or attempt:
                                raise
                            time.sleep(.25)
                except ChatGPTAuthError:
                    pass
                if not confirmed or latest_unknown:
                    self.last_warning = "本应用已退出，但未确认远端会话撤销；可在 ChatGPT 设置中断开本应用。"
            self._clear_tokens(record)
            self.store.save(state)
            self._blocked_clients.discard(record["client_id"])
