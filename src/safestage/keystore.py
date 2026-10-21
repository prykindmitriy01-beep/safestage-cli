"""Where the API key lives.

``config.json`` no longer holds the key. It is looked up, in order, in:

1. ``SAFESTAGE_API_KEY`` (read only, SafeStage never writes it),
2. the OS keyring (Windows Credential Manager, macOS Keychain, Secret Service),
3. a DPAPI-encrypted file on Windows,
4. a file with ``600`` permissions on Linux and macOS when no keyring works.

A key is written to the first backend that accepts it and returns it unchanged
on read-back. It is then removed from the other backends, so no stale copy
outlives the move. On Windows there is deliberately no plain-text fallback:
if neither the keyring nor DPAPI works, saving fails and the environment
variable is the way out.
"""

from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .paths import config_dir, write_private

SERVICE = "safestage-cli"
ENV_KEY = "SAFESTAGE_API_KEY"

_DPAPI_UI_FORBIDDEN = 0x1


class SecretStoreError(RuntimeError):
    pass


class _Backend:
    title = ""

    def load(self, provider: str) -> Optional[str]:
        raise NotImplementedError

    def store(self, provider: str, key: str) -> bool:
        raise NotImplementedError

    def remove(self, provider: str) -> None:
        raise NotImplementedError


class _Keyring(_Backend):
    @property
    def title(self) -> str:  # type: ignore[override]
        return {"win32": "Windows Credential Manager", "darwin": "macOS Keychain"}.get(
            sys.platform, "the OS keyring"
        )

    # keyring is imported lazily because backend discovery is slow on some
    # systems. Backends raise anything (no D-Bus, locked keychain, broken
    # install), so every failure here simply means "this backend is unusable".

    def load(self, provider: str) -> Optional[str]:
        try:
            import keyring

            return keyring.get_password(SERVICE, provider) or None
        except Exception:
            return None

    def store(self, provider: str, key: str) -> bool:
        try:
            import keyring

            keyring.set_password(SERVICE, provider, key)
            return keyring.get_password(SERVICE, provider) == key
        except Exception:
            return False

    def remove(self, provider: str) -> None:
        try:
            import keyring

            keyring.delete_password(SERVICE, provider)
        except Exception:
            pass


class _JsonFile(_Backend):
    """One JSON object per file: provider id -> encoded key."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def _encode(self, key: str) -> str:
        raise NotImplementedError

    def _decode(self, value: str) -> str:
        raise NotImplementedError

    def _read(self) -> Dict[str, str]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        return {name: value for name, value in raw.items() if isinstance(name, str) and isinstance(value, str)}

    def _write(self, data: Dict[str, str]) -> None:
        if data:
            write_private(self._path, json.dumps(data, indent=2) + "\n")
        else:
            self._path.unlink(missing_ok=True)

    def load(self, provider: str) -> Optional[str]:
        value = self._read().get(provider)
        if value is None:
            return None
        try:
            return self._decode(value) or None
        except Exception:
            # DPAPI blobs only decrypt for the same Windows user, a corrupt entry is just unusable
            return None

    def store(self, provider: str, key: str) -> bool:
        try:
            data = self._read()
            data[provider] = self._encode(key)
            self._write(data)
        except Exception:
            return False
        return self.load(provider) == key

    def remove(self, provider: str) -> None:
        data = self._read()
        if provider not in data:
            return
        del data[provider]
        try:
            self._write(data)
        except OSError:
            pass


def _dpapi_call(function_name: str, data: bytes) -> bytes:
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buffer = ctypes.create_string_buffer(data, len(data))
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    result = Blob()

    function = getattr(ctypes.windll.crypt32, function_name)  # type: ignore[attr-defined]
    ok = function(ctypes.byref(source), None, None, None, None, _DPAPI_UI_FORBIDDEN, ctypes.byref(result))
    if not ok:
        raise ctypes.WinError()  # type: ignore[attr-defined]
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        free = ctypes.windll.kernel32.LocalFree  # type: ignore[attr-defined]
        free.argtypes = [ctypes.c_void_p]
        free(ctypes.cast(result.pbData, ctypes.c_void_p))


def _dpapi_protect(data: bytes) -> bytes:
    return _dpapi_call("CryptProtectData", data)


def _dpapi_unprotect(data: bytes) -> bytes:
    return _dpapi_call("CryptUnprotectData", data)


class _Dpapi(_JsonFile):
    title = "a Windows DPAPI file (encrypted for your Windows account)"

    def __init__(
        self,
        path: Path,
        protect: Optional[Callable[[bytes], bytes]] = None,
        unprotect: Optional[Callable[[bytes], bytes]] = None,
    ) -> None:
        super().__init__(path)
        self._protect = protect or _dpapi_protect
        self._unprotect = unprotect or _dpapi_unprotect

    def _encode(self, key: str) -> str:
        return base64.b64encode(self._protect(key.encode("utf-8"))).decode("ascii")

    def _decode(self, value: str) -> str:
        return self._unprotect(base64.b64decode(value)).decode("utf-8")


class _Plain(_JsonFile):
    @property
    def title(self) -> str:  # type: ignore[override]
        return f"{self._path} (permissions 600, no OS keyring found)"

    def _encode(self, key: str) -> str:
        return key

    def _decode(self, value: str) -> str:
        return value


def _backends() -> List[_Backend]:
    backends: List[_Backend] = [_Keyring()]
    if os.name == "nt":
        backends.append(_Dpapi(config_dir() / "keys.dpapi"))
    else:
        backends.append(_Plain(config_dir() / "keys.json"))
    return backends


def get_key(provider: str) -> Optional[str]:
    override = os.environ.get(ENV_KEY, "").strip()
    if override:
        return override
    for backend in _backends():
        value = backend.load(provider)
        if value:
            return value
    return None


def set_key(provider: str, key: str) -> str:
    """Store the key and return a human-readable name of where it went."""
    backends = _backends()
    for index, backend in enumerate(backends):
        if backend.store(provider, key):
            for other in backends[:index] + backends[index + 1:]:
                other.remove(provider)
            return backend.title
    raise SecretStoreError(f"no secure storage is available, set {ENV_KEY} instead")


def delete_key(provider: str) -> None:
    for backend in _backends():
        backend.remove(provider)
