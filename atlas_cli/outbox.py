"""Outbox: listagem das notificações e retry com o mesmo payload."""

from __future__ import annotations

from . import control, notifications
from .store import Store

DEFAULT_LIMIT = 20
RETRY_STATUSES = notifications.RETRYABLE


def _ordered(store: Store) -> list[dict]:
    return sorted(
        store.notifications(),
        key=lambda notification: (
            str(notification.get("created_at", "")),
            str(notification.get("id", "")),
        ),
    )


def recent(store: Store, limit: int = DEFAULT_LIMIT) -> list[dict]:
    rows = _ordered(store)
    if limit > 0:
        rows = rows[-limit:]
    return rows


def listing(store: Store, limit: int = DEFAULT_LIMIT) -> str:
    rows = recent(store, limit)
    if not rows:
        return "outbox vazio\n"
    return "".join(notifications.describe(row) + "\n" for row in rows)


def _retry_one(store: Store, notification: dict, sender) -> tuple[int, str]:
    """Reenvia uma notificação com o mesmo `digest_id`/`payload`."""
    with store.transaction("outbox.retry") as tx:
        current = next(
            (item for item in tx.notifications if item.get("id") == notification.get("id")),
            None,
        )
        if current is None:
            return 0, f"{notification.get('id')}: notificação sumiu"
        item = dict(current)
        item["status"] = "pending"
        item["attempts"] = int(item.get("attempts", 0)) + 1
        item["error"] = None
        tx.update_notification(item)

    status, message_id, error = notifications.dispatch(
        sender, item.get("message") or "", {"id": item["digest_id"]}
    )

    with store.transaction("outbox.deliver") as tx:
        current = next(
            (row for row in tx.notifications if row.get("id") == item["id"]), item
        )
        record = dict(current)
        record["status"] = status
        record["message_id"] = message_id
        record["error"] = error
        if status == "sent":
            record["sent_at"] = store.now_iso
            notifications.mark_reported(tx, item, store.now_iso, store.dt)
        tx.update_notification(record)
        tx.on_commit(lambda: store.sync_db())

    if status == "sent":
        return 0, f"{item['id']} reenviado ({message_id})"
    if status == "failed":
        return 1, f"{item['id']} falhou de novo: {error}"
    return 1, f"{item['id']} continua com entrega desconhecida"


def run(
    store: Store,
    *,
    retry: bool = False,
    force: bool = False,
    limit: int = DEFAULT_LIMIT,
    sender=None,
) -> tuple[int, str]:
    if retry:
        control.assert_active(store, "outbox")
    notifications.mark_abandoned(store)
    if not retry:
        return 0, listing(store, limit)

    sender = sender or notifications.default_sender
    allowed = set(RETRY_STATUSES)
    if force:
        allowed.add("delivery_unknown")
    targets = [
        notification
        for notification in recent(store, limit)
        if notification.get("status") in allowed
    ]
    if not targets:
        return 0, listing(store, limit)

    code = 0
    lines: list[str] = []
    for notification in targets:
        result, line = _retry_one(store, notification, sender)
        code = max(code, result)
        lines.append(line)
    return code, "\n".join(lines) + "\n"
