"""Deliveries por subscriber (M1b.0e / DELIVERY-01).

Uma delivery é o vínculo `(trigger, demand)`: o trigger continua sendo a
entrada, e o ciclo de cada consumidor (claim, lease, epoch, ack) vive aqui.
Este módulo é puro: recebe linhas e devolve linhas; quem grava é o
`Store.transaction`.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .util import AtlasError, iso, parse_dt

DELIVERY_STATES = ("pending", "delivered", "acked", "failed", "skipped")
DELIVERY_STATE_SET = frozenset(DELIVERY_STATES)
ROLES = ("owner", "watcher")
TERMINAL_DEMAND_STATES = frozenset({"done", "dropped"})
LEASE_EXPIRED = "lease_expired"
DEFAULT_LEASE_MINUTES = 30


def terminal_demand_ids(demands: list[dict]) -> set[str]:
    """Demandas que não recebem mais entrega (`done`/`dropped`)."""
    return {
        demand.get("id")
        for demand in demands
        if demand.get("state") in TERMINAL_DEMAND_STATES
    }


def active_subscriptions(
    trigger: dict, subscriptions: list[dict], terminal: set[str]
) -> list[dict]:
    """Subscriptions que podem receber este trigger, na ordem do snapshot."""
    thread_id = trigger.get("thread_id")
    result: list[dict] = []
    for subscription in subscriptions:
        if not subscription.get("active"):
            continue
        if subscription.get("thread_id") != thread_id:
            continue
        demand_id = subscription.get("demand_id")
        if not demand_id or demand_id in terminal:
            continue
        result.append(subscription)
    return result


def subscriber_ids(
    trigger: dict, subscriptions: list[dict], terminal: set[str]
) -> list[str]:
    """`demand_id` distintos e ordenados, como `triggers.decide` espera."""
    return sorted(
        {
            subscription["demand_id"]
            for subscription in active_subscriptions(trigger, subscriptions, terminal)
        }
    )


def role_by_demand(subscriptions: list[dict], thread_id: str | None) -> dict[str, str]:
    """Mapa `demand_id -> role` das subscriptions ativas da thread."""
    roles: dict[str, str] = {}
    for subscription in subscriptions:
        if not subscription.get("active"):
            continue
        if subscription.get("thread_id") != thread_id:
            continue
        demand_id = subscription.get("demand_id")
        if not demand_id:
            continue
        role = subscription.get("role")
        roles[demand_id] = role if role in ROLES else "owner"
    return roles


def new_delivery(
    delivery_id: str, trigger_id: str, demand_id: str, role: str, now: str
) -> dict:
    return {
        "id": delivery_id,
        "trigger_id": trigger_id,
        "demand_id": demand_id,
        "role": role if role in ROLES else "owner",
        "state": "pending",
        "attempts": 0,
        "lease_owner": None,
        "lease_until": None,
        "lease_epoch": 0,
        "report_id": None,
        "created_at": now,
        "updated_at": now,
        "error": None,
    }


def _ident(delivery: dict) -> str:
    return str(delivery.get("id") or "?")


def claim(
    delivery: dict, owner: str, lease_minutes: int, now: datetime, now_iso: str
) -> dict:
    """`pending`/`failed` -> `delivered`, com lease e epoch novo."""
    owner = (owner or "").strip()
    if not owner:
        raise AtlasError("--owner vazio")
    if lease_minutes <= 0:
        raise AtlasError("--lease-minutes deve ser positivo")
    state = delivery.get("state")
    if state not in ("pending", "failed"):
        raise AtlasError(
            f"delivery {_ident(delivery)} não pode ser claimada no estado {state!r} "
            f"(só pending ou failed)"
        )
    item = dict(delivery)
    item["state"] = "delivered"
    item["lease_owner"] = owner
    item["lease_until"] = iso(now + timedelta(minutes=lease_minutes))
    item["lease_epoch"] = int(delivery.get("lease_epoch") or 0) + 1
    item["attempts"] = int(delivery.get("attempts") or 0) + 1
    item["updated_at"] = now_iso
    return item


def ack(
    delivery: dict,
    owner: str,
    epoch: int,
    report_id: str,
    now_iso: str,
    summary: str | None = None,
) -> dict:
    """`delivered` -> `acked`, com fencing por owner/epoch."""
    report_id = (report_id or "").strip()
    if not report_id:
        raise AtlasError("--report vazio")
    owner = (owner or "").strip()
    state = delivery.get("state")
    if state == "acked":
        if delivery.get("report_id") == report_id:
            return dict(delivery)
        raise AtlasError(
            f"delivery {_ident(delivery)} já acked com outro report: "
            f"{delivery.get('report_id')}"
        )
    if state != "delivered":
        raise AtlasError(
            f"delivery {_ident(delivery)} não pode ser acked no estado {state!r} "
            f"(só delivered)"
        )
    if delivery.get("lease_owner") != owner:
        raise AtlasError(
            f"lease de {_ident(delivery)} pertence a {delivery.get('lease_owner')!r}, "
            f"não a {owner!r}"
        )
    if int(delivery.get("lease_epoch") or 0) != epoch:
        raise AtlasError(
            f"epoch de {_ident(delivery)} é {delivery.get('lease_epoch')}, não {epoch}"
        )
    item = dict(delivery)
    item["state"] = "acked"
    item["report_id"] = report_id
    item["lease_owner"] = None
    item["lease_until"] = None
    item["error"] = None
    item["updated_at"] = now_iso
    if summary:
        item["summary"] = summary
    return item


def fail(
    delivery: dict,
    error: str,
    now_iso: str,
    owner: str | None = None,
    epoch: int | None = None,
) -> dict:
    """`pending`/`delivered` -> `failed`; owner/epoch são fencing opcional."""
    error = (error or "").strip()
    if not error:
        raise AtlasError("--error vazio")
    state = delivery.get("state")
    if state not in ("pending", "delivered"):
        raise AtlasError(
            f"delivery {_ident(delivery)} não pode falhar no estado {state!r} "
            f"(só pending ou delivered)"
        )
    if owner is not None or epoch is not None:
        if owner is None or epoch is None:
            raise AtlasError("use --owner e --epoch juntos")
        if delivery.get("lease_owner") != owner:
            raise AtlasError(
                f"lease de {_ident(delivery)} pertence a "
                f"{delivery.get('lease_owner')!r}, não a {owner!r}"
            )
        if int(delivery.get("lease_epoch") or 0) != epoch:
            raise AtlasError(
                f"epoch de {_ident(delivery)} é {delivery.get('lease_epoch')}, não {epoch}"
            )
    item = dict(delivery)
    item["state"] = "failed"
    item["attempts"] = int(delivery.get("attempts") or 0) + 1
    item["lease_owner"] = None
    item["lease_until"] = None
    item["error"] = error
    item["updated_at"] = now_iso
    return item


def retry(delivery: dict, now_iso: str) -> dict:
    """`failed` -> `pending`, limpando lease e erro."""
    if delivery.get("state") != "failed":
        raise AtlasError(
            f"delivery {_ident(delivery)} não pode ser retentada no estado "
            f"{delivery.get('state')!r} (só failed)"
        )
    item = dict(delivery)
    item["state"] = "pending"
    item["lease_owner"] = None
    item["lease_until"] = None
    item["error"] = None
    item["updated_at"] = now_iso
    return item


def is_expired(delivery: dict, now: datetime) -> bool:
    if delivery.get("state") != "delivered":
        return False
    lease_until = delivery.get("lease_until")
    if not lease_until:
        return False
    return parse_dt(lease_until) <= now


def expire(delivery: dict, now: datetime, now_iso: str) -> dict | None:
    """`delivered` com lease vencido -> `failed`; `None` se não venceu."""
    if not is_expired(delivery, now):
        return None
    item = dict(delivery)
    item["state"] = "failed"
    item["lease_owner"] = None
    item["lease_until"] = None
    item["error"] = LEASE_EXPIRED
    item["updated_at"] = now_iso
    return item


def format_line(delivery: dict) -> str:
    epoch = int(delivery.get("lease_epoch") or 0)
    owner = delivery.get("lease_owner") or "-"
    return (
        f"{delivery.get('id')} | {delivery.get('state')} | "
        f"{delivery.get('trigger_id')} | {delivery.get('demand_id')} | "
        f"{delivery.get('role')} | {owner}/{epoch} | "
        f"{delivery.get('report_id') or '-'}"
    )
