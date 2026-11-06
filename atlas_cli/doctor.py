"""Saúde mínima do estado (M1a.1). Saúde completa fica para M1a.3."""

from __future__ import annotations

from . import deliveries
from .store import SCHEMA_VERSION, Store
from .util import AtlasError, read_jsonl, sha256_text

JSONL_FILES = (
    "demands.jsonl",
    "events.jsonl",
    "kb.jsonl",
    "edges.jsonl",
    "digests.jsonl",
    "notifications.jsonl",
    "triggers.jsonl",
    "threads.jsonl",
    "subscriptions.jsonl",
    "deliveries.jsonl",
)


def check(store: Store) -> list[str]:
    problems: list[str] = []

    version = store.schema_version()
    if version != SCHEMA_VERSION:
        problems.append(
            f"schema_version {version!r}, esperado {SCHEMA_VERSION} (rode atlas migrate)"
        )

    try:
        if store._recovery_needed():
            problems.append("transações pendentes de replay")
    except AtlasError as exc:
        problems.append(str(exc))

    for name in JSONL_FILES:
        try:
            read_jsonl(store.state_dir / name)
        except AtlasError as exc:
            problems.append(f"JSONL inválido: {exc}")
    try:
        store._read_transactions()
    except AtlasError as exc:
        problems.append(f"JSONL inválido: {exc}")

    try:
        demands = store.demands()
    except AtlasError:
        demands = []
    for demand in demands:
        demand_id = demand.get("id")
        if demand.get("state") == "running":
            if not demand.get("owner") or not demand.get("lease_until"):
                problems.append(f"demanda running sem lease: {demand_id}")
        if "lease_epoch" not in demand:
            problems.append(f"demanda sem lease_epoch: {demand_id}")
        revision = int(demand.get("revision", 1))
        notified = int(demand.get("last_notified_revision", 0))
        if notified > revision:
            problems.append(
                f"last_notified_revision {notified} > revision {revision}: {demand_id}"
            )

    try:
        notifications = store.notifications()
    except AtlasError:
        notifications = []
    cursor_value = store.cursor().get("last_digest_at")
    for notification in notifications:
        status = notification.get("status")
        ident = notification.get("id")
        if status == "sent" and not notification.get("message_id"):
            problems.append(f"notificação sent sem message_id: {ident}")
        if (
            cursor_value
            and status in ("pending", "failed", "delivery_unknown")
            and str(notification.get("created_at", "")) <= str(cursor_value)
        ):
            problems.append(
                f"cursor à frente de notificação {status}: {ident}"
            )

    try:
        triggers = store.triggers()
    except AtlasError:
        triggers = []
    try:
        threads = store.threads()
    except AtlasError:
        threads = []
    try:
        subscriptions = store.subscriptions()
    except AtlasError:
        subscriptions = []

    thread_ids = {thread.get("id") for thread in threads}
    known_demands = {demand.get("id") for demand in demands}
    demand_by_id = {demand.get("id"): demand for demand in demands}
    external_keys: dict[tuple, str] = {}
    for trigger in triggers:
        ident = trigger.get("id")
        if trigger.get("thread_id") not in thread_ids:
            problems.append(f"trigger sem thread: {ident} ({trigger.get('thread_id')})")
        key = (trigger.get("source"), trigger.get("external_id"))
        if key in external_keys:
            problems.append(
                f"trigger duplicado ({key[0]}, {key[1]}): "
                f"{external_keys[key]} e {ident}"
            )
        else:
            external_keys[key] = ident
        body_path = trigger.get("body_path")
        body_sha = trigger.get("body_sha256")
        blob = store.home / body_path if body_path else None
        if body_path is None or not blob.exists():
            problems.append(f"trigger sem corpo: {ident} ({body_path or '-'})")
        if body_sha is None:
            problems.append(f"trigger sem body_sha256: {ident}")
        elif blob is not None and blob.exists():
            try:
                digest = sha256_text(blob.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                problems.append(f"corpo ilegível: {ident} ({body_path})")
            else:
                if digest != body_sha:
                    problems.append(f"corpo adulterado: {ident}")
        route = trigger.get("route") or {}
        if route.get("by") == "subscription" and not route.get("demand_ids"):
            problems.append(f"trigger routed por subscription sem demanda: {ident}")
        if (
            trigger.get("state") == "routed"
            and route.get("by") == "subscription"
            and not route.get("delivery_ids")
        ):
            problems.append(f"trigger routed por subscription sem delivery: {ident}")
        if (
            trigger.get("state") == "routed"
            and route.get("by") == "rule"
            and not route.get("area")
            and not route.get("demand_ids")
        ):
            problems.append(f"trigger routed por regra sem área nem demanda: {ident}")

    for subscription in subscriptions:
        if not subscription.get("active"):
            continue
        thread_id = subscription.get("thread_id")
        demand_id = subscription.get("demand_id")
        if thread_id not in thread_ids:
            problems.append(f"subscription ativa para thread inexistente: {thread_id}")
        if demand_id not in known_demands:
            problems.append(f"subscription ativa para demanda inexistente: {demand_id}")
        else:
            demand = demand_by_id.get(demand_id) or {}
            if demand.get("state") in deliveries.TERMINAL_DEMAND_STATES:
                problems.append(
                    f"subscription ativa para demanda terminal: {demand_id} "
                    f"({demand.get('state')})"
                )

    try:
        delivery_rows = store.deliveries()
    except AtlasError:
        delivery_rows = []
    trigger_ids = {trigger.get("id") for trigger in triggers}
    pairs: dict[tuple, str] = {}
    for row in delivery_rows:
        ident = row.get("id")
        if row.get("trigger_id") not in trigger_ids:
            problems.append(
                f"delivery sem trigger: {ident} ({row.get('trigger_id')})"
            )
        if row.get("demand_id") not in known_demands:
            problems.append(
                f"delivery sem demanda: {ident} ({row.get('demand_id')})"
            )
        key = (row.get("trigger_id"), row.get("demand_id"))
        if key in pairs:
            problems.append(
                f"delivery duplicada ({key[0]}, {key[1]}): {pairs[key]} e {ident}"
            )
        else:
            pairs[key] = ident
        state = row.get("state")
        if state not in deliveries.DELIVERY_STATE_SET:
            problems.append(f"delivery com estado desconhecido: {ident} ({state!r})")
        if state == "acked" and not row.get("report_id"):
            problems.append(f"delivery acked sem report_id: {ident}")
        if state == "delivered":
            if not row.get("lease_owner") or not row.get("lease_until"):
                problems.append(f"delivery delivered sem lease: {ident}")
        elif state in ("pending", "failed"):
            if row.get("lease_owner") or row.get("lease_until"):
                problems.append(f"delivery {state} com lease preenchido: {ident}")

    return problems
