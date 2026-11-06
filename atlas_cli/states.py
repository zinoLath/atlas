"""Máquina de estados das demandas."""

from __future__ import annotations

from .util import AtlasError, iso, parse_dt

TRANSITIONS: dict[str, tuple[str, ...]] = {
    "new": ("routed",),
    "routed": ("triaged",),
    "triaged": ("planned", "waiting_user", "done", "dropped"),
    "planned": ("ready", "dropped"),
    "ready": ("running", "snoozed", "dropped"),
    "running": (
        "done",
        "blocked",
        "failed",
        "ready",
        "waiting_review",
        "waiting_external",
    ),
    "blocked": ("ready", "dropped"),
    "failed": ("ready", "dropped"),
    "waiting_user": ("ready", "done", "dropped"),
    "waiting_review": ("done", "ready"),
    "waiting_external": ("ready",),
    "snoozed": ("ready", "dropped"),
    "done": (),
    "dropped": (),
}

ACTIVE_STATES = frozenset(TRANSITIONS) - {"done", "dropped"}
TERMINAL_STATES = frozenset({"done", "dropped"})

# Campos que existem em toda demanda e são limpos ao sair do estado dono.
STATE_FIELDS = ("wake_at", "artifact", "waiting_on", "follow_up_at")

LEASE_REVOKED_REASONS = frozenset({"lease_expired", "timed_out"})


class TransitionError(AtlasError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code=2)


def allowed_targets(state: str) -> tuple[str, ...]:
    return TRANSITIONS.get(state, ())


def _ensure_fields(demand: dict) -> None:
    demand.setdefault("lease_epoch", 0)
    for field in STATE_FIELDS:
        demand.setdefault(field, None)


def apply_transition(
    demand: dict,
    to: str,
    *,
    now: str,
    actor: str = "cli",
    reason: str | None = None,
    run_id: str | None = None,
    result: str | None = None,
    owner: str | None = None,
    lease_until: str | None = None,
    artifact: str | None = None,
    waiting_on: str | None = None,
    follow_up_at: str | None = None,
    wake_at: str | None = None,
    expected_epoch: int | None = None,
) -> dict:
    """Valida e aplica a transição; devolve o evento correspondente."""
    current = demand.get("state", "")
    if to not in TRANSITIONS:
        raise TransitionError(f"estado desconhecido: {to}")
    if to not in TRANSITIONS[current]:
        raise TransitionError(
            f"transição ilegal: {current} -> {to} "
            f"(permitidas: {', '.join(TRANSITIONS[current]) or 'nenhuma'})"
        )

    _ensure_fields(demand)
    epoch = int(demand.get("lease_epoch") or 0)
    if expected_epoch is not None and int(expected_epoch) != epoch:
        raise TransitionError(
            f"lease_epoch vencido: esperado {expected_epoch}, atual {epoch}"
        )

    if to == "running":
        if not owner or not lease_until:
            raise TransitionError("estado running exige --owner e --lease")
        parse_dt(lease_until)
    if to == "done" and not result:
        raise TransitionError("estado done exige --result")
    if to == "dropped" and not reason:
        raise TransitionError("estado dropped exige --reason")
    if to == "waiting_review" and not artifact:
        raise TransitionError("estado waiting_review exige --artifact")
    if to == "waiting_external":
        if not waiting_on:
            raise TransitionError("estado waiting_external exige --waiting-on")
        if not follow_up_at:
            raise TransitionError("estado waiting_external exige --follow-up-at")
        parse_dt(follow_up_at)
    if to == "snoozed":
        if not wake_at:
            raise TransitionError("estado snoozed exige --wake-at")
        parse_dt(wake_at)
    if current == "waiting_review" and to == "ready" and not reason:
        raise TransitionError("rejeição de revisão exige --reason (review_rejected)")

    if to == "running":
        demand["lease_epoch"] = epoch + 1
    elif (
        current == "running"
        and to == "ready"
        and actor == "system"
        and reason in LEASE_REVOKED_REASONS
    ):
        # Revogação de lease cerca o run antigo: epoch monotônico.
        demand["lease_epoch"] = epoch + 1

    event = {
        "seq": 0,
        "ts": now,
        "demand_id": demand.get("id"),
        "from": current,
        "to": to,
        "actor": actor,
        "reason": reason or "",
        "run_id": run_id or "",
        "lease_epoch": int(demand["lease_epoch"]),
    }

    demand["state"] = to
    demand["updated_at"] = now
    demand["revision"] = int(demand.get("revision", 1)) + 1

    if to == "running":
        demand["owner"] = owner
        demand["lease_until"] = iso(parse_dt(lease_until))
    elif to in {
        "done",
        "dropped",
        "blocked",
        "failed",
        "ready",
        "snoozed",
        "waiting_review",
        "waiting_external",
    }:
        demand["owner"] = None
        demand["lease_until"] = None

    if to == "done":
        demand["result"] = result
    if to == "dropped":
        demand["drop_reason"] = reason

    if to != "snoozed":
        demand["wake_at"] = None
    if to != "waiting_external":
        demand["waiting_on"] = None
        demand["follow_up_at"] = None
    if to != "waiting_review":
        demand["artifact"] = None

    if to == "snoozed":
        demand["wake_at"] = iso(parse_dt(wake_at))
    if to == "waiting_review":
        demand["artifact"] = artifact
    if to == "waiting_external":
        demand["waiting_on"] = waiting_on
        demand["follow_up_at"] = iso(parse_dt(follow_up_at))

    return event


def expired_leases(demands: list[dict], now: str) -> list[dict]:
    """Demandas running cujo lease já venceu."""
    moment = parse_dt(now)
    expired: list[dict] = []
    for demand in demands:
        if demand.get("state") != "running":
            continue
        lease = demand.get("lease_until")
        if not lease:
            continue
        try:
            if parse_dt(lease) <= moment:
                expired.append(demand)
        except AtlasError:
            expired.append(demand)
    return expired


def expire_leases(demands: list[dict], now: str) -> list[dict]:
    """running -> ready (actor=system) para leases vencidos, via apply_transition."""
    events: list[dict] = []
    for demand in expired_leases(demands, now):
        events.append(
            apply_transition(
                demand,
                "ready",
                now=now,
                actor="system",
                reason="lease_expired",
            )
        )
    return events


def wake_snoozed(demands: list[dict], now: str) -> list[dict]:
    """snoozed -> ready quando `wake_at <= now`, via apply_transition."""
    moment = parse_dt(now)
    events: list[dict] = []
    for demand in demands:
        if demand.get("state") != "snoozed":
            continue
        wake_at = demand.get("wake_at")
        if not wake_at:
            continue
        if parse_dt(wake_at) <= moment:
            events.append(
                apply_transition(
                    demand,
                    "ready",
                    now=now,
                    actor="system",
                    reason="wake_at",
                )
            )
    return events
