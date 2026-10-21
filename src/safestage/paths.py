"""Locations of SafeStage's per-user files and a helper to write them privately."""

from __future__ import annotations

import os
from pathlib import Path


def config_dir() -> Path:
    override = os.environ.get("SAFESTAGE_HOME")
    if override:
        return Path(override)
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "safestage"


def write_private(path: Path, text: str) -> None:
    """Replace ``path`` with ``text`` atomically, readable by the owner only.

    The content goes to a sibling temp file first, so a crash mid-write can never
    leave a half-written config or key file behind.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        if os.name != "nt":
            # a leftover temp file from an earlier crash keeps its old mode
            os.chmod(temp, 0o600)
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
