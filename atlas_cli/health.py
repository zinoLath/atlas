"""Saúde operacional: pausa, cursors, outbox, deliveries, backup e disco.

`collect` é puro sobre o estado em disco (exceto `disk_usage`) e
`render` só formata; o exit code fica com a CLI (`ok` global = nenhum check
com severidade `error`).
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime

from . import backup, control
from .store import Store
from .util import AtlasError, parse_dt

CURSOR_STALE_HOURS = 2
OUTBOX_PENDING_HOURS = 24
BACKUP_STALE_HOURS = 48
DISK_MIN_FREE = 2 * 1024**3
GIB = 1024**3


def _check(name: str, severity: str, detail: str) -> dict:
    return {"name": name, "ok": severity != "error", "severity": severity, "detail": detail}


def _problems(items: list[str]) -> str:
    return ", ".join(items)


def _hours(delta) -> float:
    return delta.total_seconds() / 3600


def _check_control(control_state: dict | None, error: str | None) -> dict:
    if control_state is None:
        return _check("control", "error", error or "control inválido")
    if not control_state["paused"]:
        return _check("control", "info", "ativo")
    reason = control_state["reason"]
    return _check("control", "info", f"pausado: {reason}" if reason else "pausado")


def _age_hours(dt: datetime, value) -> float | None:
    """Idade em horas, `None` sem timestamp; `ValueError` se inválido."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("timestamp não textual")
    try:
        return _hours(dt - parse_dt(value))
    except AtlasError:
        raise ValueError("timestamp inválido") from None


def _check_cursors(store: Store, dt: datetime) -> dict:
    directory = store.state_dir / "cursors"
    files = (
        sorted(path for path in directory.glob("*.json") if path.is_file())
        if directory.is_dir()
        else []
    )
    if not files:
        return _check("cursors", "info", "nenhum cursor")

    invalid: list[str] = []
    stale: list[str] = []
    no_success: list[str] = []
    no_poll: list[str] = []
    attempted: list[str] = []
    errors: list[str] = []
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            invalid.append(path.name)
            continue
        if not isinstance(data, dict):
            invalid.append(path.name)
            continue
        label = f"{data.get('account', '?')}/{data.get('mailbox', '?')}"

        # A liveness é o sucesso do poll, não a última mensagem lida. Cursors
        # antigos só têm `last_seen_at`, que continua valendo como fallback.
        success_at = data.get("last_success_at")
        if success_at is None:
            success_at = data.get("last_seen_at")
        attempt_at = data.get("last_attempt_at")
        try:
            age = _age_hours(dt, success_at)
            _age_hours(dt, attempt_at)
        except ValueError:
            invalid.append(path.name)
            continue

        if age is not None:
            if age > CURSOR_STALE_HOURS:
                stale.append(f"{label} ({age:.1f}h)")
        elif isinstance(attempt_at, str):
            no_success.append(f"{label} sem sucesso desde {attempt_at}")
        else:
            no_poll.append(label)

        # Tentativa mais nova que o último sucesso: o poll falhou (ou não
        # terminou) depois da última leitura boa.
        if isinstance(attempt_at, str) and isinstance(success_at, str):
            try:
                if parse_dt(attempt_at) > parse_dt(success_at):
                    attempted.append(f"{label} desde {attempt_at}")
            except AtlasError:
                invalid.append(path.name)
                continue

        try:
            failures = int(data.get("consecutive_errors") or 0)
        except (TypeError, ValueError):
            invalid.append(path.name)
            continue
        if failures > 0:
            errors.append(f"{label} ({failures} erros: {data.get('last_error') or '-'})")

    if invalid:
        return _check("cursors", "error", f"inválidos: {_problems(invalid)}")
    parts = []
    if stale:
        parts.append("sem leitura: " + _problems(stale))
    if no_success:
        parts.append("sem sucesso: " + _problems(no_success))
    if no_poll:
        parts.append("sem poll: " + _problems(no_poll))
    if attempted:
        parts.append("tentativa sem sucesso: " + _problems(attempted))
    if errors:
        parts.append("erros: " + _problems(errors))
    if parts:
        return _check("cursors", "warn", "; ".join(parts))
    return _check("cursors", "info", f"{len(files)} cursor(es) em dia")


def _check_outbox(store: Store, dt: datetime) -> dict:
    rows = store.notifications()
    if not rows:
        return _check("outbox", "info", "nenhuma notificação")

    broken = {"failed": [], "delivery_unknown": []}
    stale_pending: list[str] = []
    for row in rows:
        status = row.get("status")
        ident = str(row.get("id", "?"))
        if status in broken:
            broken[status].append(ident)
            continue
        if status != "pending":
            continue
        created = row.get("created_at")
        if not isinstance(created, str):
            continue
        try:
            age = _hours(dt - parse_dt(created))
        except AtlasError:
            continue
        if age > OUTBOX_PENDING_HOURS:
            stale_pending.append(f"{ident} ({age:.1f}h)")

    parts = [
        f"{status}={len(ids)} ({_problems(ids)})"
        for status, ids in broken.items()
        if ids
    ]
    if stale_pending:
        parts.append(f"pending>{OUTBOX_PENDING_HOURS}h={len(stale_pending)} ({_problems(stale_pending)})")
    if not parts:
        return _check("outbox", "info", f"{len(rows)} notificação(ões) sem pendência")
    severity = "error" if any(broken.values()) else "warn"
    return _check("outbox", severity, "; ".join(parts))


def _check_deliveries(store: Store, dt: datetime) -> dict:
    rows = store.deliveries()
    if not rows:
        return _check("deliveries", "info", "nenhuma delivery")

    expired: list[str] = []
    failed: list[str] = []
    for row in rows:
        ident = str(row.get("id", "?"))
        if row.get("state") == "failed":
            failed.append(ident)
            continue
        if row.get("state") != "delivered":
            continue
        lease_until = row.get("lease_until")
        if not isinstance(lease_until, str):
            continue
        try:
            expired_lease = parse_dt(lease_until) <= dt
        except AtlasError:
            expired.append(f"{ident} (lease inválido)")
            continue
        if expired_lease:
            expired.append(f"{ident} (até {lease_until})")

    parts = []
    if expired:
        parts.append(f"lease vencido={len(expired)} ({_problems(expired)})")
    if failed:
        parts.append(f"failed sem retry={len(failed)} ({_problems(failed)})")
    if not parts:
        return _check("deliveries", "info", f"{len(rows)} delivery(ies) sem pendência")
    return _check("deliveries", "warn", "; ".join(parts))


def _check_backup(store: Store, dt: datetime) -> dict:
    path = backup.latest(store)
    if path is None:
        return _check("backup", "warn", "nenhum backup")
    try:
        age = backup.age_hours(path, dt)
    except OSError as exc:
        return _check("backup", "warn", f"{path.name} ilegível ({exc})")
    if age > BACKUP_STALE_HOURS:
        return _check("backup", "warn", f"{path.name} velho ({age:.1f}h)")
    return _check("backup", "info", f"{path.name} ({age:.1f}h)")


def _check_disk(store: Store) -> dict:
    try:
        free = shutil.disk_usage(store.home).free
    except OSError as exc:
        return _check("disk", "warn", f"indisponível ({exc})")
    gib = free / GIB
    if free < DISK_MIN_FREE:
        return _check("disk", "warn", f"livre {gib:.1f} GiB (< 2 GiB)")
    return _check("disk", "info", f"livre {gib:.1f} GiB")


def collect(store: Store, *, now: str | None = None) -> dict:
    dt = parse_dt(now) if now else store.dt

    control_state: dict | None
    control_error: str | None = None
    try:
        control_state = control.read_control(store)
    except AtlasError as exc:
        control_state = None
        control_error = str(exc)

    checks = [
        _check_control(control_state, control_error),
        _check_cursors(store, dt),
        _check_outbox(store, dt),
        _check_deliveries(store, dt),
        _check_backup(store, dt),
        _check_disk(store),
    ]
    return {
        "ok": all(check["severity"] != "error" for check in checks),
        "paused": bool(control_state and control_state["paused"]),
        "checks": checks,
    }


def render(report: dict) -> str:
    lines: list[str] = []
    if report.get("paused"):
        lines.append("PAUSADO")
    for check in report.get("checks", []):
        lines.append(f"{check['severity']:<5} {check['name']:<10} {check['detail']}")
    return "\n".join(lines) + "\n"
