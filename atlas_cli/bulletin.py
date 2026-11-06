"""BULLETIN.md determinístico: estado operacional sem LLM."""

from __future__ import annotations

from . import watchdog
from .digest import _due_soon, demand_line
from .states import ACTIVE_STATES
from .store import Store
from .util import parse_dt, write_text

# Estados ainda em triagem: não são `ready` nem `waiting_user`, logo não
# aparecem em PRÓXIMOS/AGORA, mas o digest os inclui.
TRIAGE_STATES = ("new", "routed", "triaged", "planned")


def _section(title: str, lines: list[str]) -> list[str]:
    output = [f"## {title}"]
    output.extend(lines if lines else ["- (nenhum)"])
    output.append("")
    return output


def _rollover_line(demand: dict, now_dt) -> str:
    rollover = demand.get("rollover") or {}
    seen = rollover.get("first_seen") or demand.get("created_at")
    age = max(0.0, (now_dt - parse_dt(seen)).total_seconds() / 3600.0)
    return f"{demand_line(demand)} — {age:.1f} h desde a primeira captura, {int(rollover.get('digest_count', 0))} rollover(s)"


def build(store: Store) -> str:
    now_dt = store.dt
    demands = store.demands()
    active = [demand for demand in demands if demand.get("state") in ACTIVE_STATES]

    agora = sorted(
        (
            demand
            for demand in active
            if demand.get("state") == "waiting_user" or _due_soon(demand, now_dt)
        ),
        key=lambda demand: (demand.get("due") or "9999", demand["id"]),
    )
    triage = sorted(
        (demand for demand in active if demand.get("state") in TRIAGE_STATES),
        key=lambda demand: demand["id"],
    )
    running = sorted(
        (demand for demand in active if demand.get("state") == "running"),
        key=lambda demand: demand.get("lease_until") or "",
    )
    upcoming = sorted(
        (
            demand
            for demand in active
            if demand.get("state") == "ready"
            and demand.get("reported_at") is None
            and int((demand.get("rollover") or {}).get("digest_count", 0)) == 0
        ),
        key=lambda demand: demand["id"],
    )
    rollover_ids = {
        item.get("demand_id")
        for notification in store.notifications()
        if notification.get("status") == "rollover"
        for item in notification.get("items") or []
    }
    rolled = sorted(
        (
            demand
            for demand in active
            if int((demand.get("rollover") or {}).get("digest_count", 0)) > 0
            or demand["id"] in rollover_ids
        ),
        key=lambda demand: demand["id"],
    )

    lines = [f"# BULLETIN — {now_dt.strftime('%Y-%m-%d %H:%M')}", ""]
    lines += _section("AGORA", [demand_line(demand) for demand in agora])
    lines += _section(
        "EM EXECUÇÃO",
        [
            f"{demand_line(demand)} — owner {demand.get('owner')}, lease até {demand.get('lease_until')}"
            for demand in running
        ],
    )
    lines += _section("TRIAGEM", [demand_line(demand) for demand in triage])
    lines += _section("PRÓXIMOS", [demand_line(demand) for demand in upcoming])
    lines += _section("ROLLOVER", [_rollover_line(demand, now_dt) for demand in rolled])

    notifications = sorted(
        store.notifications(),
        key=lambda notification: (
            str(notification.get("created_at", "")),
            str(notification.get("id", "")),
        ),
    )[-5:]
    digest_lines: list[str] = []
    for notification in notifications:
        items = notification.get("items") or []
        digest_lines.append(
            f"- {notification.get('digest_id')} {notification.get('created_at')} "
            f"{notification.get('kind')} ({len(items)} itens, "
            f"status={notification.get('status')})"
        )
        hidden_items = [
            item.get("demand_id")
            for item in items[int(notification.get("displayed", len(items))) :]
        ]
        if hidden_items:
            digest_lines.append(
                f"  não exibidos: {', '.join(hidden_items)}"
            )
    lines += _section(
        "SAÚDE",
        [watchdog.format_line(item) for item in watchdog.check(store, now=store.now_iso)][
            : watchdog.BULLETIN_MAX_LINES
        ]
        or ["- ok"],
    )
    lines += _section("ÚLTIMOS DIGESTS", digest_lines)

    content = "\n".join(lines).rstrip() + "\n"
    write_text(store.home / "BULLETIN.md", content)
    return content
