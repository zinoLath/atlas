"""Watchdog determinístico: liveness e progresso, sem LLM e sem rede.

Fora do Router: só observa o estado canônico e, em `--repair`, aplica a
allowlist de reparos (`expire`: leases vencidos + snoozed acordada). Não
faz merge, não paga, não envia nada.
"""

from __future__ import annotations

import shutil

from . import control, doctor
from .states import ACTIVE_STATES, expire_leases, expired_leases, wake_snoozed
from .store import Store
from .util import AtlasError, parse_dt

STALE_RUNNING_MINUTES = 20
NOTIFICATION_STUCK_HOURS = 2
CANDIDATE_BACKLOG_HOURS = 24
CANDIDATE_BACKLOG_CRITICAL = 100
DIGEST_STALE_HOURS = 2
DISK_LOW_BYTES = 1024**3
BULLETIN_MAX_LINES = 8
KB_STUB_MAX = 10

STUCK_NOTIFICATION_STATUSES = ("pending", "failed", "delivery_unknown")


def _rows(read) -> list[dict]:
    """Leitura tolerante: estado inválido é problema do `doctor`, não crash."""
    try:
        return read()
    except AtlasError:
        return []


def _age_hours(moment, stamp) -> float | None:
    if not stamp:
        return None
    try:
        return (moment - parse_dt(str(stamp))).total_seconds() / 3600.0
    except AtlasError:
        return None


def _last_events(events: list[dict]) -> dict[str, dict]:
    latest: dict[str, dict] = {}
    for event in events:
        demand_id = event.get("demand_id")
        if not demand_id:
            continue
        current = latest.get(demand_id)
        if current is None or int(event.get("seq", 0)) >= int(current.get("seq", 0)):
            latest[demand_id] = event
    return latest


def format_line(item: dict) -> str:
    return f"- [{item['severity']}] {item['code']} {item['subject']}: {item['message']}"


def check(store: Store, *, now: str | None = None) -> list[dict]:
    moment = parse_dt(now or store.now_iso)
    items: list[dict] = []

    # 1. doctor: qualquer problema é crítico.
    for problem in doctor.check(store):
        items.append(
            {
                "code": "doctor",
                "severity": "critical",
                "subject": "doctor",
                "message": problem,
            }
        )

    demands = _rows(store.demands)
    events = _rows(store.events)
    notifications = _rows(store.notifications)
    triggers = _rows(store.triggers)
    try:
        cursor = store.cursor()
    except AtlasError:
        cursor = {}
    active = [demand for demand in demands if demand.get("state") in ACTIVE_STATES]

    # 2. leases vencidos.
    for demand in sorted(expired_leases(demands, now or store.now_iso), key=lambda d: d["id"]):
        items.append(
            {
                "code": "lease_expired",
                "severity": "warn",
                "subject": demand["id"],
                "message": f"lease vencido desde {demand.get('lease_until')}",
            }
        )

    # 3. running sem progresso.
    latest = _last_events(events)
    for demand in sorted(
        (demand for demand in demands if demand.get("state") == "running"),
        key=lambda d: d["id"],
    ):
        event = latest.get(demand["id"])
        hours = _age_hours(moment, (event or {}).get("ts"))
        if event is None:
            message = "running sem evento registrado"
        elif hours is None:
            message = f"running com evento ilegível ({event.get('ts')})"
        elif hours * 60.0 <= STALE_RUNNING_MINUTES:
            continue
        else:
            message = f"running há {int(hours * 60.0)} min sem evento"
        items.append(
            {
                "code": "stale_running",
                "severity": "warn",
                "subject": demand["id"],
                "message": message,
            }
        )

    # 4. notificações presas no outbox.
    for notification in sorted(notifications, key=lambda n: (str(n.get("created_at", "")), str(n.get("id", "")))):
        if notification.get("status") not in STUCK_NOTIFICATION_STATUSES:
            continue
        hours = _age_hours(moment, notification.get("created_at"))
        if hours is None:
            message = f"{notification.get('status')} sem created_at legível"
        elif hours <= NOTIFICATION_STUCK_HOURS:
            continue
        else:
            message = f"{notification.get('status')} há {int(hours)} h no outbox"
        items.append(
            {
                "code": "notification_stuck",
                "severity": "warn",
                "subject": str(notification.get("id")),
                "message": message,
            }
        )

    # 5. backlog de triggers candidate.
    backlog = 0
    for trigger in triggers:
        if trigger.get("state") != "candidate":
            continue
        hours = _age_hours(moment, trigger.get("received_at"))
        if hours is None or hours > CANDIDATE_BACKLOG_HOURS:
            backlog += 1
    if backlog:
        items.append(
            {
                "code": "candidate_backlog",
                "severity": "critical"
                if backlog > CANDIDATE_BACKLOG_CRITICAL
                else "warn",
                "subject": "triggers",
                "message": f"{backlog} trigger(s) candidate há mais de "
                f"{CANDIDATE_BACKLOG_HOURS} h",
            }
        )

    # 6. demanda ativa sem lane.
    for demand in sorted(
        (demand for demand in active if not demand.get("lane")), key=lambda d: d["id"]
    ):
        items.append(
            {
                "code": "demand_no_lane",
                "severity": "warn",
                "subject": demand["id"],
                "message": "demanda ativa sem lane",
            }
        )

    # 7. nó de KB stub (sem summary).
    stubs = sorted(
        (node for node in _rows(store.kb_nodes) if not node.get("summary")),
        key=lambda n: str(n.get("id")),
    )
    for node in stubs[:KB_STUB_MAX]:
        items.append(
            {
                "code": "kb_stub",
                "severity": "warn",
                "subject": str(node.get("id")),
                "message": "nó de KB sem summary",
            }
        )
    if len(stubs) > KB_STUB_MAX:
        items.append(
            {
                "code": "kb_stub",
                "severity": "warn",
                "subject": "kb",
                "message": f"{len(stubs)} nó(s) de KB sem summary",
            }
        )

    # 8. digest parado com trabalho ativo.
    last_digest = cursor.get("last_digest_at")
    if active:
        hours = _age_hours(moment, last_digest)
        if hours is None:
            items.append(
                {
                    "code": "digest_stale",
                    "severity": "warn",
                    "subject": "cursor",
                    "message": "last_digest_at ausente ou ilegível com demanda ativa",
                }
            )
        elif hours > DIGEST_STALE_HOURS:
            items.append(
                {
                    "code": "digest_stale",
                    "severity": "warn",
                    "subject": "cursor",
                    "message": f"último digest há {int(hours)} h com demanda ativa",
                }
            )

    # 9. disco.
    try:
        free = shutil.disk_usage(str(store.home)).free
    except OSError:
        free = None
    if free is not None and free < DISK_LOW_BYTES:
        items.append(
            {
                "code": "disk_low",
                "severity": "critical",
                "subject": "state",
                "message": f"{free / 1024**3:.1f} GiB livres (mínimo 1 GiB)",
            }
        )

    # 10. pausa operacional (não é erro).
    if control.is_paused(store):
        items.append(
            {
                "code": "paused",
                "severity": "warn",
                "subject": "state",
                "message": "state/control.json paused=true: operações pausadas",
            }
        )

    return items


def repair(store: Store) -> dict:
    """Allowlist: devolve leases vencidos e snoozed acordada para `ready`."""
    with store.transaction("watchdog.repair", actor="system") as tx:
        expired = expire_leases(tx.demands, store.now_iso)
        woke = wake_snoozed(tx.demands, store.now_iso)
        changed: dict[str, dict] = {}
        for event in expired + woke:
            demand = next(
                item for item in tx.demands if item.get("id") == event["demand_id"]
            )
            demand = tx.upsert_demand(demand)
            tx.append_event(event)
            changed[demand["id"]] = demand
        if changed:

            def _after_commit() -> None:
                for demand in changed.values():
                    store.render_demand(demand)
                store.sync_db()

            tx.on_commit(_after_commit)
    return {"expired": len(expired), "woke": len(woke)}


def run(store: Store, *, repair_expired: bool = False) -> tuple[int, list[dict]]:
    if repair_expired:
        repair(store)
    items = check(store)
    code = 1 if any(item["severity"] == "critical" for item in items) else 0
    return code, items
