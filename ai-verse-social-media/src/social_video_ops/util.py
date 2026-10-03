from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


class UserError(Exception):
    """An actionable, safe-to-display error."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def timestamp(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        raise UserError("Use an ISO date/time with an explicit UTC offset.") from None
    if result.tzinfo is None:
        raise UserError("Date/time must include an explicit timezone offset.")
    return result.astimezone(timezone.utc)


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def file_hash(path: Path, algorithm="sha256") -> str:
    h = hashlib.new(algorithm)
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise UserError(f"Cannot read a valid JSON object from {path.name}.") from None
    if not isinstance(data, dict):
        raise UserError(f"{path.name} must contain a JSON object.")
    return data


def atomic_write(path: Path, content: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".writing-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content.encode("utf-8") if isinstance(content, str) else content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_json(path: Path, value: Any) -> None:
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def https_url(value: str) -> str:
    if not isinstance(value, str):
        raise UserError("Expected an HTTPS URL.")
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise UserError("Expected an HTTPS URL without embedded credentials.")
    host=parsed.hostname.lower().rstrip(".")
    if host=="localhost" or host.endswith((".localhost",".local",".internal")):
        raise UserError("Expected a public HTTPS hostname.")
    try:
        address=ipaddress.ip_address(host)
    except ValueError: address=None
    if address is not None and not address.is_global: raise UserError("Expected a public HTTPS address.")
    return value

