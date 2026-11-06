"""Outbox de notificações: grava antes de enviar, retry e payload estável.

Cada notificação nasce `pending` numa transação própria, é enviada fora do
lock e só então recebe `sent`/`failed`/`delivery_unknown`. Um processo que
morra entre o envio e o registro deixa a notificação `pending` com
`attempts > 0`; o próximo comando a marca `delivery_unknown` e ela não é
reenviada automaticamente (evita duplicata).
"""

from __future__ import annotations

import json
from datetime import timedelta

from .util import iso, parse_dt, sha256_text

DISPLAY_LIMIT = 12
BACKOFF_HOURS = (24, 72, 168)
RETRYABLE = ("pending", "failed")
BRIEFING_STATUSES = ("pending", "sent", "delivery_unknown")

ABANDON_ERROR = "processo morreu entre o envio e o registro da entrega"

NOTIFY_DEFAULTS = {
    "revision": 1,
    "last_notified_revision": 0,
    "reported_at": None,
    "last_digest_id": None,
    "next_notify_at": None,
    "report_count": 0,
}


def migrate_fields(demand: dict) -> None:
    """Preenche os campos de notificação ausentes (migração e `add`)."""
    for key, value in NOTIFY_DEFAULTS.items():
        demand.setdefault(key, value)


def initial_next_notify_at(due) -> str | None:
    """`next_notify_at` de uma demanda nova: `due - 24h`, ou null."""
    if not due:
        return None
    return iso(parse_dt(due) - timedelta(hours=24))


def backoff_at(demand: dict, now_dt) -> str:
    """Próximo lembrete após reportar: 24h, 72h, 7d; teto em `due - 1h`."""
    count = int(demand.get("report_count", 0))
    hours = BACKOFF_HOURS[min(max(count, 0), len(BACKOFF_HOURS) - 1)]
    candidate = now_dt + timedelta(hours=hours)
    due = demand.get("due")
    if due:
        limit = parse_dt(due) - timedelta(hours=1)
        if limit < candidate:
            candidate = limit
    return iso(candidate)


def payload_hash(digest_id: str, kind: str, items: list[dict], message: str) -> str:
    blob = json.dumps(
        {"digest_id": digest_id, "kind": kind, "items": items, "message": message},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + sha256_text(blob)


def build(
    store,
    view,
    *,
    digest_id: str,
    kind: str,
    message: str,
    items: list[dict],
    displayed: int,
    hidden: int,
    status: str,
    attempts: int,
) -> dict:
    """Monta o registro canônico; `view` fornece ids e `upper_seq`."""
    from .store import next_sequential_id

    upper_seq = max((int(event.get("seq", 0)) for event in view.events), default=0)
    return {
        "id": next_sequential_id(view.notifications, "ntf", store.dt),
        "digest_id": digest_id,
        "kind": kind,
        "created_at": store.now_iso,
        "items": items,
        "payload_hash": payload_hash(digest_id, kind, items, message),
        "status": status,
        "attempts": int(attempts),
        "sent_at": None,
        "message_id": None,
        "error": None,
        "upper_seq": upper_seq,
        "displayed": int(displayed),
        "hidden": int(hidden),
        # Payload congelado: o retry reenvia exatamente estes bytes.
        "message": message,
    }


def default_sender(message: str, digest: dict) -> str:
    """Sender padrão: escreve a mensagem no stdout e devolve o message_id."""
    print(message)
    return f"stdout:{digest['id']}"


def dispatch(sender, message: str, digest: dict) -> tuple[str, str | None, str | None]:
    """Chama o sender e normaliza o resultado em (status, message_id, erro)."""
    try:
        result = sender(message, digest)
    except Exception as exc:  # sender arbitrário; BaseException simula crash
        return "failed", None, f"{type(exc).__name__}: {exc}"
    if result is None or result == "unknown":
        return "delivery_unknown", None, None
    return "sent", str(result), None


def mark_abandoned(store) -> int:
    """`pending` com tentativa iniciada = entrega ambígua → delivery_unknown."""
    with store.transaction("notification.abandon") as tx:
        abandoned = [
            notification
            for notification in list(tx.notifications)
            if notification.get("status") == "pending"
            and int(notification.get("attempts", 0)) > 0
        ]
        for notification in abandoned:
            item = dict(notification)
            item["status"] = "delivery_unknown"
            item["error"] = item.get("error") or ABANDON_ERROR
            tx.update_notification(item)
    return len(abandoned)


def mark_reported(tx, notification: dict, now_iso: str, now_dt) -> None:
    """Marca como reportadas apenas as demandas que foram exibidas."""
    items = notification.get("items") or []
    shown: dict[str, int] = {}
    for item in items[: int(notification.get("displayed", len(items)))]:
        demand_id = item.get("demand_id")
        if not demand_id:
            continue
        revision = int(item.get("revision", 1))
        shown[demand_id] = max(shown.get(demand_id, 0), revision)
    for demand in list(tx.demands):
        if demand.get("id") not in shown:
            continue
        demand["reported_at"] = now_iso
        demand["last_digest_id"] = notification["digest_id"]
        # A revisão congelada na notificação, não a revisão atual da demanda.
        demand["last_notified_revision"] = max(
            int(demand.get("last_notified_revision", 0)), shown[demand["id"]]
        )
        demand["next_notify_at"] = backoff_at(demand, now_dt)
        demand["report_count"] = int(demand.get("report_count", 0)) + 1
        tx.upsert_demand(demand)


def describe(notification: dict) -> str:
    items = notification.get("items") or []
    return (
        f"{notification.get('id')} | {notification.get('status')} | "
        f"{notification.get('kind')} | {len(items)} itens "
        f"(exibidos {notification.get('displayed', len(items))}, "
        f"não exibidos {notification.get('hidden', 0)}) | "
        f"tentativas {notification.get('attempts', 0)}"
    )
