"""Binary-safe, bounded construction of repository feature context."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from .errors import SessionError


DEFAULT_MAX_TEXT_BYTES = 512_000
GENERATED_CONTEXT_PARTS = frozenset({
    ".build",
    ".cache",
    ".git",
    ".playwright-cli",
    "DerivedData",
    "__pycache__",
    "cache",
    "caches",
    "node_modules",
    "output",
    "reports",
    "runtime",
})
_BINARY_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpeg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"%PDF-", "pdf"),
    (b"PK\x03\x04", "zip"),
    (b"\x7fELF", "elf"),
    (b"\xca\xfe\xba\xbe", "mach-o"),
    (b"\xcf\xfa\xed\xfe", "mach-o"),
    (b"\xfe\xed\xfa\xcf", "mach-o"),
)


class ContextReadError(SessionError):
    """A typed, path- and phase-specific context construction failure."""

    def __init__(
        self,
        *,
        relative_path: str,
        phase: str,
        classification: str,
        diagnostic: str,
    ):
        self.relative_path = relative_path
        self.phase = phase
        self.classification = classification
        self.diagnostic = diagnostic
        super().__init__(
            "context_read_error: "
            f"path={relative_path}; phase={phase}; "
            f"classification={classification}; diagnostic={diagnostic}"
        )

    def evidence(self) -> dict[str, str]:
        return {
            "path": self.relative_path,
            "phase": self.phase,
            "classification": self.classification,
            "diagnostic": self.diagnostic,
        }


@dataclass(frozen=True)
class ContextFile:
    path: str
    classification: str
    size: int
    sha256: str
    detected_type: str
    text: str | None = None
    reason: str | None = None

    def metadata(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "path": self.path,
            "detected_type": self.detected_type,
            "size": self.size,
            "sha256": self.sha256,
        }
        if self.reason is not None:
            value["reason"] = self.reason
        return value


@dataclass(frozen=True)
class ContextPack:
    rendered: str
    evidence: dict[str, Any]


def _safe_relative(value: str) -> str:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or path.as_posix() != value:
        raise ContextReadError(
            relative_path=value,
            phase="path_validation",
            classification="unsafe_path",
            diagnostic="context path is not normalized and repository-relative",
        )
    return value


def generated_context_reason(relative_path: str) -> str | None:
    parts = PurePosixPath(relative_path).parts
    matched = next((part for part in parts if part in GENERATED_CONTEXT_PARTS), None)
    return (
        f"generated_or_historical_directory:{matched}"
        if matched is not None else None
    )


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(131_072), b""):
            value.update(chunk)
    return value.hexdigest()


def _binary_type(payload: bytes) -> str | None:
    for signature, detected in _BINARY_SIGNATURES:
        if payload.startswith(signature):
            return detected
    if b"\x00" in payload:
        return "nul-containing-binary"
    if payload:
        controls = sum(
            byte < 32 and byte not in {9, 10, 12, 13}
            for byte in payload[:8192]
        )
        if controls / min(len(payload), 8192) > 0.10:
            return "control-heavy-binary"
    return None


def read_context_file(
    repository: Path,
    relative_path: str,
    *,
    phase: str,
    max_text_bytes: int = DEFAULT_MAX_TEXT_BYTES,
) -> ContextFile:
    """Classify bytes before strict UTF-8 decoding and never return binary payloads."""

    relative = _safe_relative(relative_path)
    root = repository.resolve()
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ContextReadError(
            relative_path=relative,
            phase=phase,
            classification="path_escape",
            diagnostic="context path escapes the repository",
        ) from exc
    if not candidate.is_file():
        raise ContextReadError(
            relative_path=relative,
            phase=phase,
            classification="unavailable",
            diagnostic="context file is unavailable",
        )
    try:
        size = candidate.stat().st_size
        digest = _digest(candidate)
        with candidate.open("rb") as handle:
            probe = handle.read(min(size, max_text_bytes + 1))
    except OSError as exc:
        raise ContextReadError(
            relative_path=relative,
            phase=phase,
            classification="read_failed",
            diagnostic=(
                f"{type(exc).__name__}: "
                f"errno={exc.errno if exc.errno is not None else 'unavailable'}"
            ),
        ) from exc
    detected = _binary_type(probe)
    if detected is not None:
        return ContextFile(
            relative, "binary", size, digest, detected,
            reason="binary payload represented as bounded metadata",
        )
    if size > max_text_bytes:
        return ContextFile(
            relative, "oversized", size, digest, "textual-candidate",
            reason=f"text size exceeds {max_text_bytes}-byte context limit",
        )
    try:
        text = probe.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContextReadError(
            relative_path=relative,
            phase=phase,
            classification="invalid_utf8_textual_candidate",
            diagnostic=f"strict UTF-8 decode failed at byte {exc.start}",
        ) from exc
    return ContextFile(relative, "text", size, digest, "utf-8", text=text)


def build_context_pack(
    repository: Path,
    paths: Iterable[str],
    *,
    phase: str,
    explicitly_requested: Iterable[str] = (),
    inclusion_reasons: dict[str, str] | None = None,
    excluded_categories: Iterable[str] = (),
    max_text_bytes: int = DEFAULT_MAX_TEXT_BYTES,
) -> ContextPack:
    """Render text and bounded metadata while producing fingerprinted evidence."""

    explicit = frozenset(explicitly_requested)
    rendered: list[str] = []
    included_textual: list[str] = []
    excluded_generated: list[dict[str, str]] = []
    binary_metadata: list[dict[str, Any]] = []
    oversized: list[dict[str, Any]] = []
    textual_bytes = 0
    for relative in dict.fromkeys(paths):
        generated_reason = generated_context_reason(relative)
        if generated_reason is not None and relative not in explicit:
            excluded_generated.append({"path": relative, "reason": generated_reason})
            continue
        item = read_context_file(
            repository, relative, phase=phase, max_text_bytes=max_text_bytes
        )
        if item.classification == "text":
            included_textual.append(relative)
            textual_bytes += item.size
            rendered.append(
                f"### {relative}\n\n```text\n{(item.text or '').rstrip()}\n```"
            )
        elif item.classification == "binary":
            metadata = item.metadata()
            binary_metadata.append(metadata)
            rendered.append(
                f"### {relative}\n\n```json\n"
                + json.dumps({"binary_metadata": metadata}, indent=2, sort_keys=True)
                + "\n```"
            )
        else:
            oversized.append(item.metadata())
    evidence: dict[str, Any] = {
        "schema_version": 1,
        "phase": phase,
        "file_count": (
            len(included_textual)
            + len(binary_metadata)
            + len(oversized)
        ),
        "approximate_bytes": textual_bytes,
        "included_paths": [
            *included_textual,
            *(item["path"] for item in binary_metadata),
            *(item["path"] for item in oversized),
        ],
        "excluded_categories": list(dict.fromkeys(excluded_categories)),
        "inclusion_reasons": {
            path: (inclusion_reasons or {}).get(path, "controller-selected workflow context")
            for path in [
                *included_textual,
                *(item["path"] for item in binary_metadata),
                *(item["path"] for item in oversized),
            ]
        },
        "included_textual_paths": included_textual,
        "excluded_generated_paths": excluded_generated,
        "binary_metadata_paths": binary_metadata,
        "oversized_paths": oversized,
        "typed_read_failures": [],
        "total_textual_bytes": textual_bytes,
        "approximate_textual_tokens": (textual_bytes + 3) // 4,
    }
    evidence["context_pack_fingerprint"] = hashlib.sha256(
        json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return ContextPack("\n\n".join(rendered), evidence)
