from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .keystore import ENV_KEY, SecretStoreError, delete_key, get_key, set_key
from .paths import config_dir, write_private
from .providers import PROVIDERS, Provider
from .ui import error

_migration_warned = False


@dataclass(frozen=True)
class Config:
    provider: str
    api_key: str
    base_url: Optional[str]
    model: str


def config_path() -> Path:
    return config_dir() / "config.json"


def _read_raw() -> Optional[Dict[str, Any]]:
    try:
        raw = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _migrate_legacy_key(raw: Dict[str, Any], provider: str, key: str) -> None:
    """Move a plain-text ``api_key`` from an old config.json into the key store.

    The old value is erased only after the key store returned it unchanged on
    read-back. If anything fails the file is left as it was and the key keeps
    working from there.
    """
    global _migration_warned
    try:
        destination = set_key(provider, key)
        cleaned = {name: value for name, value in raw.items() if name != "api_key"}
        write_private(config_path(), json.dumps(cleaned, indent=2, ensure_ascii=False) + "\n")
    except (SecretStoreError, OSError) as exc:
        if not _migration_warned:
            _migration_warned = True
            error(f"could not move the API key out of config.json: {exc}")
        return
    print(
        f"[safestage] API key moved from config.json to {destination}. "
        "Old copies of the file may remain on disk, rotate the key if it was ever shared or synced."
    )


def load_config() -> Optional[Config]:
    raw = _read_raw()
    if raw is None:
        return None

    provider = raw.get("provider")
    model = raw.get("model")
    base_url = raw.get("base_url")
    if not (isinstance(provider, str) and provider in PROVIDERS):
        return None
    if not (isinstance(model, str) and model):
        return None

    legacy = raw.get("api_key")
    legacy_key = legacy if isinstance(legacy, str) and legacy else None
    if legacy_key:
        _migrate_legacy_key(raw, provider, legacy_key)
    api_key = get_key(provider) or legacy_key
    if not api_key:
        return None

    url = base_url if isinstance(base_url, str) and base_url else None
    if PROVIDERS[provider].custom_url and url is None:
        return None
    return Config(provider, api_key, url, model)


def save_config(config: Config) -> str:
    """Save settings and return where the API key was stored."""
    previous = (_read_raw() or {}).get("provider")
    destination = set_key(config.provider, config.api_key)
    payload = {"provider": config.provider, "base_url": config.base_url, "model": config.model}
    write_private(config_path(), json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    if isinstance(previous, str) and previous != config.provider:
        delete_key(previous)
    return destination


def _apply(config: Config) -> Optional[Config]:
    try:
        destination = save_config(config)
    except (OSError, SecretStoreError) as exc:
        error(f"config write failed: {exc}")
        return None
    print(f"[safestage] Saved: {PROVIDERS[config.provider].title} / {config.model}, API key in {destination}")
    if os.environ.get(ENV_KEY, "").strip():
        print(f"[safestage] Note: {ENV_KEY} is set and takes priority over the saved key.")
    return config


def _pick(prompt: str, count: int) -> Optional[int]:
    while True:
        raw = input(prompt).strip()
        if raw == "0":
            return None
        if raw.isdigit() and 1 <= int(raw) <= count:
            return int(raw)
        print(f"Enter a number from 1 to {count}, or 0 to go back")


def read_secret(prompt: str) -> str:
    if not sys.stdin.isatty():
        return input(prompt)

    sys.stdout.write(prompt)
    sys.stdout.flush()
    chars: List[str] = []

    def echo(text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()

    if os.name == "nt":
        import msvcrt

        while True:
            char = msvcrt.getwch()
            if char in ("\r", "\n"):
                break
            if char == "\x03":
                echo("\n")
                raise KeyboardInterrupt
            if char in ("\x00", "\xe0"):
                msvcrt.getwch()
            elif char == "\b":
                if chars:
                    chars.pop()
                    echo("\b \b")
            elif char.isprintable():
                chars.append(char)
                echo("*")
    else:
        import termios
        import tty

        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            while True:
                char = sys.stdin.read(1)
                if char in ("\r", "\n"):
                    break
                if char == "\x03":
                    raise KeyboardInterrupt
                if char in ("\x04", ""):
                    raise EOFError
                if char in ("\x7f", "\b"):
                    if chars:
                        chars.pop()
                        echo("\b \b")
                elif char.isprintable():
                    chars.append(char)
                    echo("*")
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)

    echo("\n")
    return "".join(chars)


def _select_model(provider: Provider) -> Optional[str]:
    while True:
        print("Select model:")
        for index, (label, _) in enumerate(provider.models, 1):
            print(f"  {index}. {label}")
        custom = len(provider.models) + 1
        print(f"  {custom}. Custom model id")
        print("  0. Back")
        number = _pick("Model: ", custom)
        if number is None:
            return None
        if number != custom:
            return provider.models[number - 1][1]
        model = input("Model id (0 to go back): ").strip()
        if model and model != "0":
            return model


def _select_base_url(previous: Optional[str]) -> Optional[str]:
    hint = ", Enter to keep current" if previous else ""
    while True:
        value = input(f"Base URL, e.g. https://host/v1 (0 to go back{hint}): ").strip()
        if value == "0":
            return None
        if not value and previous:
            return previous
        if value.startswith(("http://", "https://")):
            return value
        print("URL must start with http:// or https://")


def _select_key(previous: str) -> Optional[str]:
    hint = "0 to go back, Enter to keep current" if previous else "0 to go back"
    while True:
        value = read_secret(f"API key ({hint}): ").strip()
        if value == "0":
            return None
        if value:
            return value
        if previous:
            return previous


def configure(current: Optional[Config]) -> Optional[Config]:
    keys = list(PROVIDERS)
    provider = PROVIDERS[keys[0]]
    key = keys[0]
    base_url: Optional[str] = None
    model = ""
    step = "provider"

    try:
        while True:
            if step == "provider":
                print("Select provider:")
                for index, name in enumerate(keys, 1):
                    print(f"  {index}. {PROVIDERS[name].title}")
                print("  0. Cancel")
                number = _pick("Provider: ", len(keys))
                if number is None:
                    return None
                key = keys[number - 1]
                provider = PROVIDERS[key]
                base_url = provider.base_url
                step = "url" if provider.custom_url else "model"
            elif step == "url":
                previous_url = current.base_url if current and current.provider == key else None
                value = _select_base_url(previous_url)
                if value is None:
                    step = "provider"
                else:
                    base_url = value
                    step = "model"
            elif step == "model":
                chosen = _select_model(provider)
                if chosen is None:
                    step = "url" if provider.custom_url else "provider"
                else:
                    model = chosen
                    step = "key"
            else:
                previous_key = current.api_key if current and current.provider == key else ""
                api_key = _select_key(previous_key)
                if api_key is None:
                    step = "model"
                else:
                    return _apply(Config(key, api_key, base_url, model))
    except (EOFError, KeyboardInterrupt):
        print()
        return None


def change_model(current: Config) -> Optional[Config]:
    try:
        model = _select_model(PROVIDERS[current.provider])
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    return _apply(replace(current, model=model)) if model else None


def edit_config(current: Optional[Config]) -> Optional[Config]:
    if current is None:
        return configure(None)

    provider = PROVIDERS[current.provider]
    actions: List[Tuple[str, str]] = [
        ("provider", "Change provider"),
        ("model", "Change model"),
        ("key", "Change API key"),
    ]
    if provider.custom_url:
        actions.insert(2, ("url", "Change base URL"))

    try:
        print(f"Current: {provider.title} / {current.model}" + (f" / {current.base_url}" if provider.custom_url else ""))
        for index, (_, title) in enumerate(actions, 1):
            print(f"  {index}. {title}")
        print("  0. Cancel")
        number = _pick("Option: ", len(actions))
        if number is None:
            return None

        action = actions[number - 1][0]
        if action == "provider":
            return configure(current)
        if action == "model":
            return change_model(current)
        if action == "key":
            api_key = _select_key(current.api_key)
            return _apply(replace(current, api_key=api_key)) if api_key else None
        url = _select_base_url(current.base_url)
        return _apply(replace(current, base_url=url)) if url else None
    except (EOFError, KeyboardInterrupt):
        print()
        return None
