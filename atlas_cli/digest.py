"""Digest horário: elegibilidade, score, rollover, briefing e outbox.

O envio é em duas transações: a notificação `pending` é gravada e commitada
**antes** de chamar o sender; o resultado vira `sent`/`failed`/
`delivery_unknown` numa segunda transação. Só o `sent` avança o cursor e
marca as demandas exibidas como reportadas.
"""

from __future__ import annotations

from datetime import timedelta

from . import notifications, policy
from .store import Store
from .util import parse_dt


def demand_line(demand: dict) -> str:
    classification = demand.get("classification") or {}
    importance = classification.get("importance", 3)
    line = f"- {demand['id']} {demand.get('area', '')}: {demand.get('title', '')} (imp {importance}"
    due = demand.get("due")
    if due:
        line += f", vence {parse_dt(due).strftime('%d/%m')}"
    return line + ")"


def _due_soon(demand: dict, now_dt) -> bool:
    due = demand.get("due")
    if not due:
        return False
    return parse_dt(due) <= now_dt + timedelta(hours=24)


def score_of(demand: dict, now_dt) -> int:
    classification = demand.get("classification") or {}
    score = 2 * int(classification.get("importance", 3))
    score += int(classification.get("urgency", 1))
    if demand.get("state") == "waiting_user":
        score += 3
    if _due_soon(demand, now_dt):
        score += 2
    if demand.get("state") == "blocked":
        score += 1
    return score


def _first_seen(demand: dict):
    rollover = demand.get("rollover") or {}
    stamp = rollover.get("first_seen") or demand.get("created_at")
    return parse_dt(stamp) if stamp else None


def _age_hours(demand: dict, now_dt) -> float:
    seen = _first_seen(demand)
    if seen is None:
        return 0.0
    return max(0.0, (now_dt - seen).total_seconds() / 3600.0)


def is_candidate(demand: dict, now_dt) -> bool:
    """Elegibilidade da rodada: mudança, lembrete vencido ou alerta."""
    state = demand.get("state")
    if state in ("done", "dropped", "failed"):
        return False
    if state == "snoozed":
        wake_at = demand.get("wake_at")
        if not wake_at or parse_dt(wake_at) > now_dt:
            return False

    revision = int(demand.get("revision", 1))
    changed = revision > int(demand.get("last_notified_revision", 0))
    next_notify_at = demand.get("next_notify_at")
    reminder = bool(next_notify_at) and parse_dt(next_notify_at) <= now_dt
    waiting = state == "waiting_user" and changed
    due_soon = _due_soon(demand, now_dt) and changed
    return changed or reminder or waiting or due_soon


def render_message(now_dt, scored: list[tuple[dict, int]], hidden: int = 0) -> str:
    header = f"ATLAS {now_dt.strftime('%H:%M')} — {len(scored) + hidden} itens"
    lines = [header, ""]
    if not scored:
        lines.append("Nada novo.")
    else:
        urgent = [
            (demand, score)
            for demand, score in scored
            if demand.get("state") == "waiting_user" or _due_soon(demand, now_dt)
        ]
        rest = [item for item in scored if item not in urgent]
        if urgent:
            lines.append("AGORA")
            lines.extend(demand_line(demand) for demand, _ in urgent)
        if rest:
            if urgent:
                lines.append("")
            lines.append("FILA")
            lines.extend(demand_line(demand) for demand, _ in rest)
    if hidden:
        lines.extend(["", f"… {hidden} itens não exibidos (atlas outbox)"])
    lines.extend(
        [
            "",
            "comandos: atlas list --state ready | atlas bulletin | atlas show <id>",
        ]
    )
    return "\n".join(lines)


def _plan(store: Store, view, *, force: bool) -> dict | None:
    """Calcula candidatos, score e a decisão da rodada (sem gravar nada)."""
    now_dt = store.dt
    digest_cfg = policy.load_policy(store.home).get("digest", {})
    threshold = int(digest_cfg.get("threshold", 7))
    rollover_max_hours = float(digest_cfg.get("rollover_max_hours", 12))
    briefing_hour = int(digest_cfg.get("briefing_hour", 8))

    candidates = [
        demand for demand in view.demands if is_candidate(demand, now_dt)
    ]
    scored = sorted(
        ((demand, score_of(demand, now_dt)) for demand in candidates),
        key=lambda item: (-item[1], item[0]["id"]),
    )
    displayed = scored[: notifications.DISPLAY_LIMIT]
    hidden = scored[notifications.DISPLAY_LIMIT :]

    today = now_dt.date().isoformat()
    briefing_exists = any(
        notification.get("kind") == "briefing"
        and str(notification.get("created_at", ""))[:10] == today
        and notification.get("status") in notifications.BRIEFING_STATUSES
        for notification in view.notifications
    )
    briefing_due = now_dt.hour >= briefing_hour and not briefing_exists

    if briefing_due:
        kind, send, reason = "briefing", True, "briefing"
    elif force:
        kind, send, reason = "digest", True, "force"
    elif not scored:
        return None
    else:
        kind = "digest"
        max_score = max(score for _, score in scored)
        oldest = max(_age_hours(demand, now_dt) for demand in candidates)
        if max_score < threshold and oldest < rollover_max_hours:
            send, reason = False, "rollover"
        else:
            send = True
            reason = "age" if max_score < threshold else "threshold"

    return {
        "now_dt": now_dt,
        "kind": kind,
        "send": send,
        "reason": reason,
        "scored": scored,
        "displayed": displayed,
        "hidden": hidden,
        "max_score": max((score for _, score in scored), default=0),
        "message": render_message(now_dt, displayed, len(hidden)) if send else "",
    }


def _digest_record(store: Store, view, plan: dict) -> dict:
    return {
        "id": view.next_digest_id(),
        "kind": plan["kind"],
        "sent": 1 if plan["send"] else 0,
        "created_at": store.now_iso,
        "items": [
            {"id": demand["id"], "score": score} for demand, score in plan["scored"]
        ],
        "max_score": plan["max_score"],
        "reason": plan["reason"],
    }


def _notification_items(plan: dict) -> list[dict]:
    return [
        {
            "demand_id": demand["id"],
            "revision": int(demand.get("revision", 1)),
            "score": score,
        }
        for demand, score in plan["scored"]
    ]


def _apply_rollover(tx, plan: dict) -> None:
    for demand in plan["scored"]:
        item = demand[0]
        rollover = item.setdefault(
            "rollover",
            {"digest_count": 0, "first_seen": item.get("created_at")},
        )
        rollover["digest_count"] = int(rollover.get("digest_count", 0)) + 1
        tx.upsert_demand(item)


def _after_commit(store: Store) -> None:
    """Re-renderiza as vistas derivadas (roda sob o lock, após o commit)."""
    from . import bulletin

    for demand in store.demands():
        store.render_demand(demand)
    bulletin.build(store)
    store.sync_db()


def _deliver(store: Store, notification: dict, sender) -> tuple[int, str]:
    status, message_id, error = notifications.dispatch(
        sender, notification.get("message") or "", {"id": notification["digest_id"]}
    )
    with store.transaction("digest.deliver") as tx:
        current = {item.get("id"): item for item in tx.notifications}
        record = dict(current.get(notification["id"], notification))
        record["status"] = status
        record["message_id"] = message_id
        record["error"] = error
        if status == "sent":
            record["sent_at"] = store.now_iso
            notifications.mark_reported(tx, notification, store.now_iso, store.dt)
            tx.set_cursor("last_digest_at", store.now_iso)
        tx.update_notification(record)
        tx.on_commit(lambda: _after_commit(store))

    if status == "failed":
        return 1, f"falha no envio: {error}"
    if status == "delivery_unknown":
        return 1, (
            "entrega desconhecida: notificação gravada como delivery_unknown "
            "(use atlas outbox --retry --force)"
        )
    return 0, ""


def run(
    store: Store,
    *,
    force: bool = False,
    dry_run: bool = False,
    sender=None,
) -> tuple[int, str]:
    if dry_run:
        plan = _plan(store, store.snapshot(), force=force)
        return 0, (plan["message"] if plan else "")

    sender = sender or notifications.default_sender
    # `pending` com tentativa iniciada de uma execução morta: ambíguo.
    notifications.mark_abandoned(store)

    with store.transaction("digest.plan") as tx:
        plan = _plan(store, tx, force=force)
        if plan is None:
            return 0, ""
        digest = _digest_record(store, tx, plan)
        tx.append_digest(digest)
        if not plan["send"]:
            _apply_rollover(tx, plan)
            tx.put_notification(
                notifications.build(
                    store,
                    tx,
                    digest_id=digest["id"],
                    kind=plan["kind"],
                    message="",
                    items=_notification_items(plan),
                    displayed=len(plan["displayed"]),
                    hidden=len(plan["hidden"]),
                    status="rollover",
                    attempts=0,
                )
            )
            tx.on_commit(lambda: _after_commit(store))
            return 0, ""

        notification = notifications.build(
            store,
            tx,
            digest_id=digest["id"],
            kind=plan["kind"],
            message=plan["message"],
            items=_notification_items(plan),
            displayed=len(plan["displayed"]),
            hidden=len(plan["hidden"]),
            status="pending",
            attempts=1,
        )
        tx.put_notification(notification)
        tx.on_commit(lambda: _after_commit(store))

    return _deliver(store, notification, sender)
