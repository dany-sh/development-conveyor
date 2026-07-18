"""Centralized deterministic redaction before persistence."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

REDACTED = "[REDACTED]"
SENSITIVE_QUERY_KEYS = {"access_token", "api_key", "apikey", "auth", "authorization", "cookie", "key", "secret", "signature", "sig", "token"}

PATTERNS = (
    (re.compile(r"(?im)^.*(?:www[_-]?authenticate|authrequirederror|authrequired\().*$"), "[REDACTED_AUTH_CHALLENGE]"),
    (re.compile(r"(?i)(authorization\s*:\s*(?:bearer|basic)\s+)[^\s,;]+"), rf"\1{REDACTED}"),
    (re.compile(r"(?i)((?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password|passwd|cookie|set-cookie)\s*[=:]\s*)[^\s,;]+"), rf"\1{REDACTED}"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL), "[REDACTED_PRIVATE_KEY]"),
    (re.compile(r"(?i)\b(?:ghp|github_pat|sk|xox[baprs])[-_][A-Za-z0-9_-]{12,}\b"), REDACTED),
    (re.compile(r"/Users/[^/]+/Library/CloudStorage/[^\s\"']+"), "[REDACTED_USER_DATA_PATH]"),
)


def _redact_urls(text: str) -> str:
    url_pattern = re.compile(r"https?://[^\s\"'<>]+")

    def replace(match: re.Match[str]) -> str:
        raw = match.group(0)
        try:
            parsed = urlsplit(raw)
            host = parsed.hostname or ""
            port = f":{parsed.port}" if parsed.port else ""
            netloc = f"{host}{port}"
            pairs = []
            for key, value in parse_qsl(parsed.query, keep_blank_values=True):
                pairs.append((key, REDACTED if key.lower() in SENSITIVE_QUERY_KEYS else value))
            return urlunsplit((parsed.scheme, netloc, parsed.path, urlencode(pairs), parsed.fragment))
        except ValueError:
            return "[REDACTED_URL]"

    return url_pattern.sub(replace, text)


def redact_text(text: str) -> str:
    value = _redact_urls(text)
    for pattern, replacement in PATTERNS:
        value = pattern.sub(replacement, value)
    return value


def redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item) for item in value)
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if key.lower() in SENSITIVE_QUERY_KEYS or any(word in key.lower() for word in ("password", "private_key", "credential")):
                result[key] = REDACTED
            else:
                result[key] = redact_value(item)
        return result
    return value
