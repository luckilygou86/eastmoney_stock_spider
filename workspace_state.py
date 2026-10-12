"""Small, local UI preferences; no quotes, exports or credentials are cached here."""
from __future__ import annotations

import json
import math
import os
import re
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

PROJECT_ROOT = Path(__file__).resolve().parent
STATE_PATH = PROJECT_ROOT / ".stock_spider_state.json"
REFRESH_OPTIONS = (5, 10, 15, 30, 60)


def _ai_text(value: Any, limit: int) -> str:
    if not isinstance(value, str) or len(value.strip()) > limit or any(ord(char) < 32 or ord(char) == 127 for char in value):
        return ""
    return value.strip()


def _ai_seconds(value: Any, default: int, low: int, high: int) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(number) or not low <= number <= high:
        return default
    return int(number) if number.is_integer() else number


def _public_base_url(value: Any) -> str:
    """Never persist a URL that embeds a credential, query or fragment."""
    text = _ai_text(value, 2048)
    if not text:
        return ""
    try:
        parsed = urlsplit(text)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username is not None or parsed.password is not None or "?" in text or "#" in text or "\\" in text or any(char.isspace() for char in text):
            return ""
        if parsed.scheme == "http" and parsed.hostname.lower() not in {"127.0.0.1", "localhost", "::1"}:
            return ""
        parsed.port  # Reject malformed port numbers while preserving the URL.
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))
    except ValueError:
        return ""


def _unique_text(values: Any, limit: int, codes_only: bool = False) -> list[str]:
    if not isinstance(values, list):
        return []
    result = []
    for value in values:
        if not isinstance(value, str):
            continue
        value = value.strip()[:160]
        if not value or any(ord(char) < 32 for char in value):
            continue
        if codes_only and not re.fullmatch(r"(?:sh|sz|bj)?\d{6}", value.lower()):
            continue
        if value not in result:
            result.append(value)
        if len(result) >= limit:
            break
    return result


def sanitize_preferences(data: Any) -> dict[str, Any]:
    data = data if isinstance(data, dict) else {}
    try:
        refresh = int(data.get("refresh_seconds", 15))
    except (TypeError, ValueError, OverflowError):
        refresh = 15
    try:
        daily_range = int(data.get("daily_range", 60))
    except (TypeError, ValueError, OverflowError):
        daily_range = 60
    export_dir = data.get("last_export_dir", "")
    chart_mode = data.get("chart_mode")
    auth_mode = data.get("ai_auth_mode")
    protocol = data.get("ai_protocol")
    return {
        "recent_queries": _unique_text(data.get("recent_queries"), 15),
        "watchlist": _unique_text(data.get("watchlist"), 30, codes_only=True),
        "refresh_seconds": refresh if refresh in REFRESH_OPTIONS else 15,
        "last_export_dir": export_dir[:4096] if isinstance(export_dir, str) and "\x00" not in export_dir else "",
        "chart_mode": chart_mode if isinstance(chart_mode, str) and chart_mode in {"分时", "日线"} else "日线",
        "daily_range": daily_range if daily_range in {30, 60, 120, 250} else 60,
        "ai_auth_mode": auth_mode if isinstance(auth_mode, str) and auth_mode in {"api_key", "chatgpt"} else "api_key",
        "ai_base_url": _public_base_url(data.get("ai_base_url")),
        "ai_model": _ai_text(data.get("ai_model"), 240),
        "ai_protocol": protocol if isinstance(protocol, str) and protocol in {"chat_completions", "responses"} else "chat_completions",
        "ai_stream_timeout": _ai_seconds(data.get("ai_stream_timeout"), 600, 30, 1800),
        "ai_idle_timeout": _ai_seconds(data.get("ai_idle_timeout"), 120, 5, 300),
    }


def load_preferences(path: Path | None = None) -> dict[str, Any]:
    path = Path(path) if path is not None else STATE_PATH
    try:
        if path.stat().st_size > 65536:
            return sanitize_preferences({})
        return sanitize_preferences(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, UnicodeError, RecursionError):
        return sanitize_preferences({})


def save_preferences(data: dict[str, Any], path: Path | None = None) -> bool:
    path = Path(path) if path is not None else STATE_PATH
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".tmp", prefix=".stock-ui-", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(sanitize_preferences(data), handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        return True
    except (OSError, ValueError):
        return False
    finally:
        if temporary is not None and temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass
