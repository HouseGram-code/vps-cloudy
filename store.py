"""Tiny JSON store for bot state (banned users)."""

import json
from pathlib import Path

DATA_FILE = Path(__file__).parent / "data.json"


def _load() -> dict:
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"banned": []}


def _save(data: dict) -> None:
    DATA_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def is_banned(user_id) -> bool:
    return str(user_id) in _load().get("banned", [])


def ban(user_id) -> None:
    d = _load()
    if str(user_id) not in d.get("banned", []):
        d.setdefault("banned", []).append(str(user_id))
        _save(d)


def unban(user_id) -> None:
    d = _load()
    if str(user_id) in d.get("banned", []):
        d["banned"].remove(str(user_id))
        _save(d)


def banned() -> list:
    return _load().get("banned", [])
