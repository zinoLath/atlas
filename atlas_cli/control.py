"""Pausa operacional: readers e retry do outbox param; o resto continua.

`state/control.json` é o único arquivo tocado por `pause`/`resume`. A pausa
não é criptográfica: comandos manuais (`health`, `backup`, `resume`) seguem
funcionando, e quem bloqueia é `assert_active` chamado nos pontos de entrada
automáticos (`imap_reader.ingest_mailbox`, `outbox.run --retry`).
"""

from __future__ import annotations

import json

from .store import Store
from .util import AtlasError, parse_dt, write_text

DEFAULTS = {"paused": False, "reason": None, "updated_at": None}


def _normalize(data: dict) -> dict:
    """Valida e devolve apenas as chaves conhecidas, na ordem canônica."""
    if not isinstance(data, dict):
        raise AtlasError("control inválido: esperado objeto JSON")
    paused = data.get("paused", False)
    if not isinstance(paused, bool):
        raise AtlasError("control inválido: paused deve ser booleano")
    reason = data.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise AtlasError("control inválido: reason deve ser string ou null")
    updated_at = data.get("updated_at")
    if updated_at is not None:
        if not isinstance(updated_at, str):
            raise AtlasError("control inválido: updated_at deve ser string ou null")
        parse_dt(updated_at)
    return {"paused": paused, "reason": reason, "updated_at": updated_at}


def path(store: Store):
    return store.state_dir / "control.json"


def read_control(store: Store) -> dict:
    target = path(store)
    if not target.exists():
        return dict(DEFAULTS)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AtlasError(f"control inválido: {target}") from exc
    return _normalize(data)


def write_control(store: Store, control: dict):
    item = _normalize(control)
    text = json.dumps(item, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    target = path(store)
    # Mesmo motivo do cursor: `backup` só arquiva sob o lock dos escritores.
    with store.lock():
        write_text(target, text)
    return target


def pause(store: Store, reason: str | None, now: str | None = None) -> dict:
    if reason is not None and not isinstance(reason, str):
        raise AtlasError("motivo inválido")
    text = (reason or "").strip() or None
    control = {
        "paused": True,
        "reason": text,
        "updated_at": now or store.now_iso,
    }
    write_control(store, control)
    return _normalize(control)


def resume(store: Store, now: str | None = None) -> dict:
    control = {
        "paused": False,
        "reason": None,
        "updated_at": now or store.now_iso,
    }
    write_control(store, control)
    return _normalize(control)


def is_paused(store: Store) -> bool:
    return read_control(store)["paused"]


def assert_active(store: Store, action: str) -> None:
    """Levanta `AtlasError` se o atlas estiver pausado."""
    control = read_control(store)
    if not control["paused"]:
        return
    reason = control["reason"]
    suffix = f" ({reason})" if reason else ""
    raise AtlasError(f"atlas pausado: {action} bloqueado{suffix}")
