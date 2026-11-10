"""Keychain access and the settings file behind RuntimeAPI.update_settings."""

from __future__ import annotations

import asyncio
import os
import tomllib
from collections.abc import Collection
from pathlib import Path
from typing import Any

import keyring
import keyring.errors
import tomli_w

from kayden.api import KEYCHAIN_SERVICE

SETTINGS_FILE = "settings.toml"


def read_key(name: str) -> str | None:
    """The key from the environment, else the keychain; None if neither has it. Blocks:
    call it off the event loop."""
    if value := os.environ.get(name):
        return value
    try:
        return keyring.get_password(KEYCHAIN_SERVICE, name)
    except keyring.errors.KeyringError:
        return None


async def has_key(name: str | None) -> bool:
    return name is not None and await asyncio.to_thread(read_key, name) is not None


async def get_stored_key(name: str) -> str | None:
    try:
        return await asyncio.to_thread(keyring.get_password, KEYCHAIN_SERVICE, name)
    except keyring.errors.KeyringError:
        return None


async def store_key(name: str, value: str | None) -> None:
    """Write (or, with None, remove) a keychain entry. ValueError without a keychain."""
    try:
        if value is None:
            await asyncio.to_thread(keyring.delete_password, KEYCHAIN_SERVICE, name)
        else:
            await asyncio.to_thread(keyring.set_password, KEYCHAIN_SERVICE, name, value)
    except keyring.errors.PasswordDeleteError:
        pass
    except keyring.errors.KeyringError as e:
        raise ValueError(f"no keychain available ({e}); set {name} in the environment") from e


def save_settings(data_dir: Path, changes: dict[str, Any], *, tables: Collection[str] = ()
                  ) -> None:
    """Merge `changes` into the settings file and replace it atomically. A change to one
    of `tables` merges into that table."""
    path = data_dir / SETTINGS_FILE
    try:
        saved = tomllib.loads(path.read_text())
    except FileNotFoundError:
        saved = {}
    for key, value in changes.items():
        saved[key] = {**saved.get(key, {}), **value} if key in tables else value
    tmp = path.with_suffix(".tmp")
    tmp.write_text(tomli_w.dumps(saved, multiline_strings=True))
    os.replace(tmp, path)
