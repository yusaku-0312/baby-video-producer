"""Read only the owner-edited project .env.local; never read legacy stores."""
from __future__ import annotations

import os
from pathlib import Path
import stat

from .state import PipelineError

ALLOWED_KEYS = {"FAL_KEY", "FISH_API_KEY"}
ENVIRONMENT_KEYS = ALLOWED_KEYS | {"FISH_AUDIO_API_KEY"}
TEMPLATE = "# 本人がこのファイルに入力。共有・Git・ZIPへの追加は禁止。\nFAL_KEY=\nFISH_API_KEY=\n"


def check_private(path: Path):
    if path.is_symlink():
        raise PipelineError("Local environment file cannot be a symbolic link")
    metadata = path.stat()
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise PipelineError("Local environment file must be owner-only; use chmod 600 .env.local")
    if not stat.S_ISREG(metadata.st_mode):
        raise PipelineError("Local environment input must be a regular file")


def prepare_template(root: Path) -> bool:
    """Create blank fields once, preserving every byte of an existing file."""
    path = root / ".env.local"
    if path.exists() or path.is_symlink():
        check_private(path)
        return False
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(TEMPLATE)
        stream.flush()
        os.fsync(stream.fileno())
    return True


def load_credentials(root: Path) -> dict:
    path = root / ".env.local"
    if not path.exists() and not path.is_symlink():
        return {}
    check_private(path)
    if path.stat().st_size > 65536:
        raise PipelineError("Local environment file is outside the size limit")
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError):
        raise PipelineError("Local environment file could not be read; no values were logged") from None
    values = {}
    seen = set()
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:]
        key, separator, value = line.partition("=")
        if not separator:
            raise PipelineError("Invalid local environment syntax; no values were logged")
        key = key.strip()
        if key not in ALLOWED_KEYS:
            continue
        if key in seen:
            raise PipelineError("Duplicate credential field; no values were logged")
        seen.add(key)
        value = value.strip()
        if value and value[0] in "\"'":
            if len(value) < 2 or value[-1] != value[0]:
                raise PipelineError("Invalid quoted credential; no values were logged")
            value = value[1:-1]
        if not value:
            continue
        if not 8 <= len(value) <= 2048 or any(c.isspace() or ord(c) < 32 for c in value):
            raise PipelineError("Credential length or whitespace is invalid; no values were logged")
        values[key] = value
    return values


def activate_credentials(root: Path):
    values = load_credentials(root)
    # Inherited variables and old JSON are never alternative inputs.
    for key in ENVIRONMENT_KEYS:
        os.environ.pop(key, None)
    os.environ.update(values)


def credential_presence(root: Path):
    values = load_credentials(root)
    return {key: bool(values.get(key)) for key in sorted(ALLOWED_KEYS)}
