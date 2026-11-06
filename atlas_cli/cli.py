"""CLI do ATLAS: argparse + dispatch determinístico."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import (
    backup,
    bulletin,
    context,
    control,
    daemon,
    deliveries,
    digest,
    doctor,
    health,
    imap_reader,
    ingest,
    kb,
    notifications,
    outbox,
    policy,
    signal as signal_sender,
    triggers,
    watchdog,
)
from .states import (
    ACTIVE_STATES,
    TERMINAL_STATES,
    apply_transition,
    expire_leases,
    wake_snoozed,
)
from .store import Store
from .util import AtlasError, iso, parse_dt, write_text


def _common_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--now",
        default=argparse.SUPPRESS,
        metavar="ISO8601",
        help="instante de referência (default: agora)",
    )
    return common


def build_parser() -> argparse.ArgumentParser:
    common = _common_parser()
    parser = argparse.ArgumentParser(
        prog="atlas",
        description="ATLAS — demandas, base de conhecimento e digest determinísticos.",
    )
    parser.add_argument("--now", default=None, metavar="ISO8601")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", parents=[common], help="cria o layout e a policy padrão")

    add = sub.add_parser("add", parents=[common], help="cria uma demanda em new")
    add.add_argument("--area", required=True)
    add.add_argument("--title", required=True)
    add.add_argument("--due")
    add.add_argument("--estimate-min", type=int, default=0, dest="estimate_min")
    add.add_argument("--source-reader", default="", dest="source_reader")
    add.add_argument("--source-ref", default="", dest="source_ref")
    add.add_argument("--source-item-hash", default="", dest="source_item_hash")
    add.add_argument("--kb", action="append", default=[], dest="kb_links")
    add.add_argument("--related", action="append", default=[])
    add.add_argument("--artifact", action="append", default=[])
    add.add_argument("--body-file", dest="body_file")
    _add_classification_flags(add)

    listing = sub.add_parser("list", parents=[common], help="lista demandas")
    listing.add_argument("--state", action="append", choices=sorted(ACTIVE_STATES | {"done", "dropped"}))
    listing.add_argument("--area")

    show = sub.add_parser("show", parents=[common], help="mostra uma demanda em JSON")
    show.add_argument("demand_id")

    move = sub.add_parser("transition", parents=[common], help="aplica uma transição de estado")
    move.add_argument("demand_id")
    move.add_argument("to")
    move.add_argument("--actor", default="cli")
    move.add_argument("--reason")
    move.add_argument("--result")
    move.add_argument("--run-id", dest="run_id")
    move.add_argument("--owner")
    move.add_argument("--lease")
    move.add_argument("--artifact")
    move.add_argument("--waiting-on", dest="waiting_on")
    move.add_argument("--follow-up-at", dest="follow_up_at")
    move.add_argument("--wake-at", dest="wake_at")
    move.add_argument("--expected-epoch", type=int, dest="expected_epoch")

    classify = sub.add_parser("classify", parents=[common], help="atualiza classificação e lane")
    classify.add_argument("demand_id")
    _add_classification_flags(classify)

    sub.add_parser("expire", parents=[common], help="devolve running com lease vencido para ready")

    events = sub.add_parser("events", parents=[common], help="lista eventos (opcionalmente de uma demanda)")
    events.add_argument("demand_id", nargs="?")

    query = sub.add_parser("q", parents=[common], help="busca na base de conhecimento")
    query.add_argument("query")
    query.add_argument("--area")
    query.add_argument("--limit", type=int, default=5)

    sub.add_parser("rebuild", parents=[common], help="replay das transações + vistas markdown")
    sub.add_parser("bulletin", parents=[common], help="gera BULLETIN.md")
    sub.add_parser("migrate", parents=[common], help="migra o estado v1 para o schema 2")
    sub.add_parser("doctor", parents=[common], help="verifica a saúde mínima do estado")

    paused = sub.add_parser(
        "pause", parents=[common], help="pausa readers e retry do outbox"
    )
    paused.add_argument("--reason", help="motivo da pausa")
    sub.add_parser("resume", parents=[common], help="retoma readers e outbox")

    healthy = sub.add_parser("health", parents=[common], help="saúde operacional")
    healthy.add_argument("--json", action="store_true", help="relatório em JSON")

    snapshot = sub.add_parser("backup", parents=[common], help="backup do estado")
    snapshot.add_argument("--out", dest="out_dir", help="diretório de saída")
    snapshot.add_argument("--keep", type=int, default=backup.DEFAULT_KEEP)

    watch = sub.add_parser(
        "watchdog", parents=[common], help="liveness e progresso (determinístico)"
    )
    watch.add_argument("--repair", action="store_true", help="aplica a allowlist de reparos")
    watch.add_argument("--json", action="store_true", help="lista completa em JSON")

    dig = sub.add_parser("digest", parents=[common], help="executa o digest horário")
    dig.add_argument("--force", action="store_true")
    dig.add_argument("--dry-run", action="store_true", dest="dry_run")

    box = sub.add_parser("outbox", parents=[common], help="notificações e retry do envio")
    box.add_argument("--retry", action="store_true")
    box.add_argument("--force", action="store_true")
    box.add_argument("--limit", type=int, default=outbox.DEFAULT_LIMIT)
    box.add_argument(
        "--sender",
        choices=("stdout", "signal"),
        default="stdout",
        help="sender do retry (default: stdout)",
    )

    kb_parser = sub.add_parser("kb", parents=[common], help="base de conhecimento")
    kb_sub = kb_parser.add_subparsers(dest="kb_command", required=True)

    kb_add = kb_sub.add_parser("add", parents=[common], help="cria um nó")
    kb_add.add_argument("--parent", default="")
    kb_add.add_argument("--kind", required=True)
    kb_add.add_argument("--title", required=True)
    kb_add.add_argument("--summary", required=True)
    kb_add.add_argument("--tags", default="")
    kb_add.add_argument("--importance", type=int, default=3)
    kb_add.add_argument("--body-file", dest="body_file")

    kb_link = kb_sub.add_parser("link", parents=[common], help="cria uma aresta")
    kb_link.add_argument("src")
    kb_link.add_argument("rel")
    kb_link.add_argument("dst")
    kb_link.add_argument("--weight", type=float, default=kb.REL_DEFAULT_WEIGHT)
    kb_link.add_argument("--note", default="")

    kb_tree = kb_sub.add_parser("tree", parents=[common], help="imprime a subárvore")
    kb_tree.add_argument("node_id")

    kb_path = kb_sub.add_parser("path", parents=[common], help="imprime o caminho até o nó")
    kb_path.add_argument("node_id")

    kb_related = kb_sub.add_parser("related", parents=[common], help="lista arestas do nó")
    kb_related.add_argument("node_id")

    kb_sub.add_parser("check", parents=[common], help="valida a KB")
    kb_sub.add_parser("index", parents=[common], help="recria o índice FTS")
    kb_sub.add_parser("dup", parents=[common], help="detecta títulos duplicados")

    trigger = sub.add_parser("trigger", parents=[common], help="triggers de entrada")
    trigger_sub = trigger.add_subparsers(dest="trigger_command", required=True)

    trigger_add = trigger_sub.add_parser("add", parents=[common], help="grava um trigger")
    trigger_add.add_argument("--source", required=True)
    trigger_add.add_argument("--external-id", required=True, dest="external_id")
    trigger_add.add_argument("--title", required=True)
    trigger_add.add_argument("--thread-key", dest="thread_key")
    trigger_add.add_argument("--actor", default="")
    trigger_add.add_argument("--kind")
    trigger_add.add_argument("--body")
    trigger_add.add_argument("--body-file", dest="body_file")
    trigger_add.add_argument("--received-at", dest="received_at")
    trigger_add.add_argument("--label", action="append", default=[])
    trigger_add.add_argument("--trusted", action="store_true")

    trigger_list = trigger_sub.add_parser("list", parents=[common], help="lista triggers")
    trigger_list.add_argument("--state", action="append", choices=sorted(triggers.TRIGGER_STATES))
    trigger_list.add_argument("--source")
    trigger_list.add_argument("--thread")
    trigger_list.add_argument("--limit", type=int, default=20)
    trigger_list.add_argument("--json", action="store_true")

    trigger_route = trigger_sub.add_parser("route", parents=[common], help="fast path determinístico")
    trigger_route.add_argument("--limit", type=int, default=100)
    trigger_route.add_argument("--dry-run", action="store_true", dest="dry_run")
    trigger_route.add_argument("--explain", action="store_true")

    trigger_move = trigger_sub.add_parser("transition", parents=[common], help="muda o estado de um trigger")
    trigger_move.add_argument("trigger_id")
    trigger_move.add_argument("to")

    subscribe = sub.add_parser("subscribe", parents=[common], help="inscreve uma demanda numa thread")
    subscribe.add_argument("demand_id")
    subscribe.add_argument("thread_id")
    subscribe.add_argument("--role", choices=("owner", "watcher"), default="owner")

    unsubscribe = sub.add_parser("unsubscribe", parents=[common], help="desativa a inscrição")
    unsubscribe.add_argument("demand_id")
    unsubscribe.add_argument("thread_id")

    thread = sub.add_parser("thread", parents=[common], help="threads de entrada")
    thread_sub = thread.add_subparsers(dest="thread_command", required=True)
    thread_list = thread_sub.add_parser("list", parents=[common], help="lista threads")
    thread_list.add_argument("--open", action="store_true")
    thread_list.add_argument("--limit", type=int, default=20)

    delivery = sub.add_parser(
        "delivery", parents=[common], help="entregas por subscriber"
    )
    delivery_sub = delivery.add_subparsers(dest="delivery_command", required=True)

    delivery_list = delivery_sub.add_parser(
        "list", parents=[common], help="lista deliveries"
    )
    delivery_list.add_argument(
        "--state", action="append", choices=sorted(deliveries.DELIVERY_STATE_SET)
    )
    delivery_list.add_argument("--demand")
    delivery_list.add_argument("--trigger")
    delivery_list.add_argument("--limit", type=int, default=20)
    delivery_list.add_argument("--json", action="store_true")

    delivery_claim = delivery_sub.add_parser(
        "claim", parents=[common], help="assume a entrega com lease e epoch"
    )
    delivery_claim.add_argument("delivery_id")
    delivery_claim.add_argument("--owner", required=True)
    delivery_claim.add_argument(
        "--lease-minutes", type=int, default=deliveries.DEFAULT_LEASE_MINUTES,
        dest="lease_minutes",
    )

    delivery_ack = delivery_sub.add_parser(
        "ack", parents=[common], help="confirma a entrega com report"
    )
    delivery_ack.add_argument("delivery_id")
    delivery_ack.add_argument("--owner", required=True)
    delivery_ack.add_argument("--epoch", type=int, required=True)
    delivery_ack.add_argument("--report", required=True)
    delivery_ack.add_argument("--summary")

    delivery_fail = delivery_sub.add_parser(
        "fail", parents=[common], help="marca a entrega como falha"
    )
    delivery_fail.add_argument("delivery_id")
    delivery_fail.add_argument("--error", required=True)
    delivery_fail.add_argument("--owner")
    delivery_fail.add_argument("--epoch", type=int)

    delivery_retry = delivery_sub.add_parser(
        "retry", parents=[common], help="devolve delivery failed para pending"
    )
    delivery_retry.add_argument("delivery_id")

    delivery_sub.add_parser(
        "expire", parents=[common], help="falha leases vencidos"
    )

    ingest_parser = sub.add_parser(
        "ingest", parents=[common], help="ingestão de fontes externas"
    )
    ingest_sub = ingest_parser.add_subparsers(dest="ingest_command", required=True)
    ingest_email = ingest_sub.add_parser(
        "email", parents=[common], help="lê a mailbox IMAP em shadow"
    )
    ingest_email.add_argument("--account", default="usuario")
    ingest_email.add_argument("--mailbox")
    ingest_email.add_argument("--limit", type=int, default=50)
    ingest_email.add_argument("--since", metavar="YYYY-MM-DD")
    ingest_email.add_argument(
        "--unseen", action=argparse.BooleanOptionalAction, default=True
    )
    ingest_email.add_argument("--dry-run", action="store_true", dest="dry_run")

    shadow_parser = sub.add_parser(
        "shadow", parents=[common], help="relatório do modo shadow"
    )
    shadow_sub = shadow_parser.add_subparsers(dest="shadow_command", required=True)
    shadow_report = shadow_sub.add_parser(
        "report", parents=[common], help="conta o dia e grava reports/shadow"
    )
    shadow_report.add_argument("--date", metavar="YYYY-MM-DD")

    ctx = sub.add_parser(
        "context", parents=[common], help="context pack da demanda (somente leitura)"
    )
    ctx.add_argument("demand_id")
    ctx.add_argument("--budget", type=int, default=context.DEFAULT_BUDGET)
    ctx.add_argument("--expand", metavar="ID")
    ctx.add_argument("--json", action="store_true")

    return parser


def _add_classification_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--difficulty", type=int)
    parser.add_argument("--complexity", type=int)
    parser.add_argument("--importance", type=int)
    parser.add_argument("--urgency", type=int)
    parser.add_argument("--responsibility")
    parser.add_argument("--agent-level", type=int, dest="agent_level")
    parser.add_argument("--confidence", type=float)
    parser.add_argument("--rationale")


def _classification(args, base: dict | None = None) -> dict:
    classification = dict(policy.DEFAULT_CLASSIFICATION if base is None else base)
    for key in ("difficulty", "complexity", "importance", "urgency", "agent_level", "confidence"):
        value = getattr(args, key, None)
        if value is not None:
            classification[key] = value
    if getattr(args, "responsibility", None) is not None:
        classification["responsibility"] = args.responsibility
    if getattr(args, "rationale", None) is not None:
        classification["rationale"] = args.rationale
    if not 1 <= int(classification["importance"]) <= 5:
        raise AtlasError("importance deve estar entre 1 e 5")
    return classification


def _find_demand(demands: list[dict], demand_id: str) -> dict:
    for demand in demands:
        if demand.get("id") == demand_id:
            return demand
    raise AtlasError(f"demanda inexistente: {demand_id}")


def _after_demand_write(store: Store, demand: dict, body_file: str | None = None) -> None:
    """Vistas derivadas da demanda; roda sob o lock, depois do commit."""
    if body_file:
        with open(body_file, "r", encoding="utf-8") as fh:
            write_text(store.demand_md(demand["id"]), fh.read())
    store.render_demand(demand)
    store.sync_db()


def cmd_init(store: Store, args) -> int:
    store.init()
    print(f"layout pronto em {store.home}")
    return 0


def cmd_add(store: Store, args) -> int:
    if args.area not in policy.AREAS:
        raise AtlasError(f"área inválida: {args.area} (use {', '.join(policy.AREAS)})")
    title = args.title.strip()
    if not title:
        raise AtlasError("título vazio")
    due = iso(parse_dt(args.due)) if args.due else None

    with store.transaction("demand.add") as tx:
        nodes = {node["id"]: node for node in tx.kb_nodes}
        for node_id in args.kb_links:
            if node_id not in nodes:
                raise AtlasError(f"nó de KB inexistente no link: {node_id}")
        known_ids = {demand.get("id") for demand in tx.demands}
        for related_id in args.related:
            if related_id not in known_ids:
                raise AtlasError(f"demanda relacionada inexistente: {related_id}")

        classification = _classification(args)
        lane = policy.compute_lane(classification, policy.load_policy(store.home))

        demand = {
            "id": tx.next_demand_id(),
            "area": args.area,
            "title": title,
            "state": "new",
            "created_at": store.now_iso,
            "updated_at": store.now_iso,
            "source": {
                "reader": args.source_reader,
                "ref": args.source_ref,
                "item_hash": args.source_item_hash,
            },
            "links": {
                "kb": list(args.kb_links),
                "related": list(args.related),
                "artifacts": list(args.artifact),
            },
            "due": due,
            "estimate_min": int(args.estimate_min),
            "classification": classification,
            "lane": lane,
            "next_action": None,
            "owner": None,
            "lease_until": None,
            "lease_epoch": 0,
            "wake_at": None,
            "artifact": None,
            "waiting_on": None,
            "follow_up_at": None,
            "rollover": {"digest_count": 0, "first_seen": store.now_iso},
        }
        notifications.migrate_fields(demand)
        demand["next_notify_at"] = notifications.initial_next_notify_at(due)
        demand["revision"] = 1

        demand = tx.upsert_demand(demand)
        tx.append_event(
            {
                "seq": 0,
                "ts": store.now_iso,
                "demand_id": demand["id"],
                "from": "",
                "to": "new",
                "actor": "cli",
                "reason": "",
                "run_id": "",
            }
        )
        tx.on_commit(lambda: _after_demand_write(store, demand, args.body_file))

    print(f"{demand['id']} {demand['state']} {demand['area']}: {demand['title']}")
    return 0


def cmd_list(store: Store, args) -> int:
    demands = store.demands()
    if args.state:
        demands = [demand for demand in demands if demand.get("state") in args.state]
    if args.area:
        demands = [demand for demand in demands if demand.get("area") == args.area]
    demands.sort(key=lambda demand: (demand.get("created_at", ""), demand["id"]))
    for demand in demands:
        print(
            f"{demand['id']} | {demand.get('state')} | {demand.get('lane') or '-'} | "
            f"{demand.get('area')} | {demand.get('title')}"
        )
    return 0


def cmd_show(store: Store, args) -> int:
    demand = _find_demand(store.demands(), args.demand_id)
    print(json.dumps(demand, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_transition(store: Store, args) -> int:
    with store.transaction("demand.transition") as tx:
        demand = _find_demand(tx.demands, args.demand_id)
        event = apply_transition(
            demand,
            args.to,
            now=store.now_iso,
            actor=args.actor,
            reason=args.reason,
            run_id=args.run_id,
            result=args.result,
            owner=args.owner,
            lease_until=args.lease,
            artifact=args.artifact,
            waiting_on=args.waiting_on,
            follow_up_at=args.follow_up_at,
            wake_at=args.wake_at,
            expected_epoch=args.expected_epoch,
        )
        demand = tx.upsert_demand(demand)
        tx.append_event(event)
        if demand.get("state") in TERMINAL_STATES:
            _close_terminal(tx, demand["id"], store.now_iso)
        tx.on_commit(lambda: _after_demand_write(store, demand))
    print(f"{demand['id']} {event['from']} -> {demand['state']}")
    return 0


#: Deliveries que ainda podem virar `skipped` quando a demanda termina.
OPEN_DELIVERY_STATES = frozenset({"pending", "delivered", "failed"})


def _close_terminal(tx, demand_id: str, now_iso: str) -> None:
    """Demanda terminal não mantém inscrição ativa nem entrega em aberto."""
    for row in list(tx.subscriptions):
        if row.get("demand_id") != demand_id or not row.get("active"):
            continue
        subscription = dict(row)
        subscription["active"] = False
        subscription["updated_at"] = now_iso
        tx.put_subscription(subscription)
    for row in list(tx.deliveries):
        if row.get("demand_id") != demand_id:
            continue
        if row.get("state") not in OPEN_DELIVERY_STATES:
            continue
        delivery = dict(row)
        delivery["state"] = "skipped"
        delivery["updated_at"] = now_iso
        delivery["error"] = "demand_terminal"
        tx.put_delivery(delivery)


def cmd_classify(store: Store, args) -> int:
    with store.transaction("demand.classify") as tx:
        demand = _find_demand(tx.demands, args.demand_id)
        classification = _classification(args, demand.get("classification") or {})
        demand["classification"] = classification
        demand["lane"] = policy.compute_lane(classification, policy.load_policy(store.home))
        demand["updated_at"] = store.now_iso
        demand["revision"] = int(demand.get("revision", 1)) + 1
        demand = tx.upsert_demand(demand)
        tx.on_commit(lambda: _after_demand_write(store, demand))
    print(f"{demand['id']} lane={demand['lane']}")
    return 0


def cmd_expire(store: Store, args) -> int:
    with store.transaction("demand.expire") as tx:
        expired = expire_leases(tx.demands, store.now_iso)
        woke = wake_snoozed(tx.demands, store.now_iso)
        events = expired + woke
        changed: dict[str, dict] = {}
        for event in events:
            demand = tx.upsert_demand(_find_demand(tx.demands, event["demand_id"]))
            tx.append_event(event)
            changed[demand["id"]] = demand
        if events:
            tx.on_commit(
                lambda: (_render_all(store, changed), store.sync_db())
            )
    print(f"{len(expired)} lease(s) expirado(s)")
    print(f"{len(woke)} snoozed acordada(s)")
    return 0


def _render_all(store: Store, demands: dict[str, dict]) -> None:
    for demand in demands.values():
        store.render_demand(demand)


def cmd_events(store: Store, args) -> int:
    events = store.events()
    if args.demand_id:
        events = [event for event in events if event.get("demand_id") == args.demand_id]
    for event in sorted(events, key=lambda item: int(item.get("seq", 0))):
        print(
            f"{event.get('seq'):04d} {event.get('ts')} {event.get('tx_id') or '-'} "
            f"{event.get('demand_id')} {event.get('from')} -> {event.get('to')} "
            f"[{event.get('actor')}] {event.get('reason') or ''}".rstrip()
        )
    return 0


def cmd_q(store: Store, args) -> int:
    results = kb.search(store, args.query, area=args.area, limit=args.limit)
    if not results:
        print("nenhum resultado")
        return 0
    for result in results:
        print(f"{result['id']} | {result['title']} | {result['snippet']} | {result['why']}")
    return 0


def cmd_rebuild(store: Store, args) -> int:
    store.rebuild()
    print(f"rebuild ok: {len(store.demands())} demandas, {len(store.kb_nodes())} nós")
    return 0


def cmd_bulletin(store: Store, args) -> int:
    print(bulletin.build(store), end="")
    return 0


def cmd_digest(store: Store, args) -> int:
    code, message = digest.run(store, force=args.force, dry_run=args.dry_run)
    if message:
        print(message)
    return code


def cmd_outbox(store: Store, args) -> int:
    sender = (
        signal_sender.send if args.sender == "signal" else notifications.default_sender
    )
    code, message = outbox.run(
        store, retry=args.retry, force=args.force, limit=args.limit, sender=sender
    )
    print(message, end="")
    return code


def _after_kb_write(store: Store, node: dict, body: str | None) -> None:
    if body is not None:
        write_text(store.kb_md(node["id"]), body.rstrip() + "\n")
    store.render_kb(node)
    store.sync_db()


def cmd_kb(store: Store, args) -> int:
    if args.kb_command == "add":
        body = None
        if args.body_file:
            with open(args.body_file, "r", encoding="utf-8") as fh:
                body = fh.read()
        tags = [tag.strip() for tag in args.tags.split(",") if tag.strip()]
        with store.transaction("kb.add") as tx:
            node = kb.add_node(
                tx,
                parent=args.parent,
                kind=args.kind,
                title=args.title,
                summary=args.summary,
                tags=tags,
                importance=args.importance,
            )
            tx.on_commit(lambda: _after_kb_write(store, node, body))
        print(node["id"])
        return 0
    if args.kb_command == "link":
        with store.transaction("kb.link") as tx:
            edge = kb.link(
                tx,
                args.src,
                args.rel,
                args.dst,
                weight=args.weight,
                note=args.note,
            )
            tx.on_commit(store.sync_db)
        print(f"{edge['src']} -{edge['rel']}-> {edge['dst']} (peso {edge['weight']:.2f})")
        return 0
    if args.kb_command == "tree":
        print(kb.tree(store, args.node_id))
        return 0
    if args.kb_command == "path":
        print(kb.path(store, args.node_id))
        return 0
    if args.kb_command == "related":
        print(kb.related(store, args.node_id))
        return 0
    if args.kb_command == "check":
        problems = kb.check(store)
        if not problems:
            print("kb ok")
            return 0
        for problem in problems:
            print(f"- {problem}")
        return 1
    if args.kb_command == "index":
        print(f"índice ok: {kb.index(store)} nós")
        return 0
    if args.kb_command == "dup":
        duplicates = kb.dup(store)
        if not duplicates:
            print("nenhum duplicado")
            return 0
        for left, right, similarity in duplicates:
            print(f"{left} | {right} | {similarity:.2f}")
        return 0
    raise AtlasError(f"subcomando kb desconhecido: {args.kb_command}")


def cmd_migrate(store: Store, args) -> int:
    if store.is_migrated():
        print("schema 2 já aplicado")
        return 0
    ops = store.migrate()
    print(f"schema 2 aplicado ({ops} ops na transação de bootstrap)")
    return 0


def cmd_pause(store: Store, args) -> int:
    control.pause(store, args.reason)
    print("paused=true")
    return 0


def cmd_resume(store: Store, args) -> int:
    control.resume(store)
    print("paused=false")
    return 0


def cmd_health(store: Store, args) -> int:
    report = health.collect(store)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(health.render(report), end="")
    return 0 if report["ok"] else 1


def cmd_backup(store: Store, args) -> int:
    result = backup.create(
        store,
        out_dir=Path(args.out_dir) if args.out_dir else None,
        keep=args.keep,
    )
    print(
        f"backup={result['path']} entries={result['entries']} bytes={result['bytes']}"
    )
    return 0


def cmd_doctor(store: Store, args) -> int:
    problems = doctor.check(store)
    if not problems:
        print("doctor ok")
        return 0
    for problem in problems:
        print(f"- {problem}")
    return 1


def cmd_watchdog(store: Store, args) -> int:
    code, items = watchdog.run(store, repair_expired=args.repair)
    if args.json:
        print(json.dumps(items, ensure_ascii=False, indent=2, sort_keys=True))
    elif items:
        for item in items:
            print(watchdog.format_line(item))
    else:
        print("watchdog ok")
    return code


def _find_trigger(rows: list[dict], trigger_id: str) -> dict:
    for trigger in rows:
        if trigger.get("id") == trigger_id:
            return trigger
    raise AtlasError(f"trigger inexistente: {trigger_id}")


def _find_thread(rows: list[dict], thread_id: str) -> dict:
    for thread in rows:
        if thread.get("id") == thread_id:
            return thread
    raise AtlasError(f"thread inexistente: {thread_id}")


def _route_new(
    rows: list[dict], subscriptions: list[dict], demands: list[dict], rules: dict
) -> list[tuple]:
    pending = [trigger for trigger in rows if trigger.get("state") == "new"]
    pending.sort(key=lambda trigger: (str(trigger.get("received_at", "")), str(trigger.get("id", ""))))
    terminal = deliveries.terminal_demand_ids(demands)
    return [
        (
            trigger,
            triggers.decide(
                trigger,
                deliveries.subscriber_ids(trigger, subscriptions, terminal),
                rules,
            ),
        )
        for trigger in pending
    ]


def _route_deliveries(
    tx, trigger: dict, demand_ids: list[str], now: str
) -> list[str]:
    """Cria (ou reaproveita) uma delivery por `(trigger, demand)`."""
    roles = deliveries.role_by_demand(tx.subscriptions, trigger.get("thread_id"))
    existing = {
        (row.get("trigger_id"), row.get("demand_id")): row.get("id")
        for row in tx.deliveries
    }
    ids: list[str] = []
    for demand_id in demand_ids:
        key = (trigger["id"], demand_id)
        if key in existing:
            ids.append(existing[key])
            continue
        item = deliveries.new_delivery(
            tx.next_delivery_id(),
            trigger["id"],
            demand_id,
            roles.get(demand_id, "owner"),
            now,
        )
        tx.put_delivery(item)
        existing[key] = item["id"]
        ids.append(item["id"])
    return ids


def _route_line(trigger: dict, decision: dict) -> str:
    route = decision["route"]
    demands = ",".join(route["demand_ids"]) or "-"
    return (
        f"{trigger['id']} {decision['state']} by={route['by'] or '-'} "
        f"rule={route['rule'] or '-'} area={route['area'] or '-'} demands={demands}"
    )


def cmd_trigger_add(store: Store, args) -> int:
    if args.body is not None and args.body_file:
        raise AtlasError("use --body ou --body-file, não ambos")
    body = ""
    if args.body_file:
        try:
            with open(args.body_file, "r", encoding="utf-8") as fh:
                body = fh.read()
        except OSError as exc:
            raise AtlasError(f"corpo ilegível: {exc}") from exc
    elif args.body is not None:
        body = args.body

    spec = {
        "source": args.source,
        "external_id": args.external_id,
        "title": args.title,
        "body": body,
        "actor": args.actor,
        "kind": args.kind,
        "received_at": args.received_at,
        "labels": list(args.label),
    }
    if args.thread_key:
        spec["thread_key"] = args.thread_key
    if args.trusted:
        spec["trusted"] = True

    trigger = ingest.add_trigger(store, spec, actor="cli")
    suffix = " reparado" if trigger.get(ingest.REPAIRED_KEY) else ""
    print(
        f"{trigger['id']} thread={trigger['thread_id']} "
        f"state={trigger['state']}{suffix}"
    )
    return 0


def cmd_trigger_list(store: Store, args) -> int:
    rows = store.triggers()
    if args.state:
        rows = [trigger for trigger in rows if trigger.get("state") in args.state]
    if args.source:
        rows = [trigger for trigger in rows if trigger.get("source") == args.source]
    if args.thread:
        rows = [trigger for trigger in rows if trigger.get("thread_id") == args.thread]
    rows.sort(
        key=lambda trigger: (str(trigger.get("received_at", "")), str(trigger.get("id", ""))),
        reverse=True,
    )
    if args.limit >= 0:
        rows = rows[: args.limit]
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    for trigger in rows:
        route = trigger.get("route") or {}
        by = route.get("by") or "-"
        area = route.get("area") or "-"
        print(
            f"{trigger.get('id')} | {trigger.get('state')} | {trigger.get('source')} | "
            f"{trigger.get('thread_id')} | {by}/{area} | {trigger.get('title')}"
        )
    return 0


def cmd_trigger_route(store: Store, args) -> int:
    rules = triggers.load_rules(store.home)
    counts = {"routed": 0, "ignored": 0, "candidate": 0}
    lines: list[str] = []

    if args.dry_run:
        decisions = _route_new(
            store.triggers(), store.subscriptions(), store.demands(), rules
        )
        if args.limit >= 0:
            decisions = decisions[: args.limit]
        for trigger, decision in decisions:
            counts[decision["state"]] += 1
            lines.append(_route_line(trigger, decision))
    else:
        with store.transaction("trigger.route") as tx:
            decisions = _route_new(
                tx.triggers, tx.subscriptions, tx.demands, rules
            )
            if args.limit >= 0:
                decisions = decisions[: args.limit]
            for trigger, decision in decisions:
                item = dict(trigger)
                triggers.transition(item, decision["state"], store.now_iso)
                route = decision["route"]
                if route.get("by") == "subscription":
                    route["delivery_ids"] = _route_deliveries(
                        tx, item, route["demand_ids"], store.now_iso
                    )
                else:
                    route["delivery_ids"] = []
                item["route"] = route
                tx.put_trigger(item)
                counts[decision["state"]] += 1
                lines.append(_route_line(item, decision))

    if args.dry_run or args.explain:
        for line in lines:
            print(line)
    summary = (
        f"routed={counts['routed']} ignored={counts['ignored']} "
        f"candidate={counts['candidate']}"
    )
    print(f"{summary} dry-run" if args.dry_run else summary)
    return 0


def cmd_trigger(store: Store, args) -> int:
    if args.trigger_command == "add":
        return cmd_trigger_add(store, args)
    if args.trigger_command == "list":
        return cmd_trigger_list(store, args)
    if args.trigger_command == "route":
        return cmd_trigger_route(store, args)
    if args.trigger_command == "transition":
        with store.transaction("trigger.transition") as tx:
            trigger = dict(_find_trigger(tx.triggers, args.trigger_id))
            previous = trigger.get("state", "")
            triggers.transition(trigger, args.to, store.now_iso)
            tx.put_trigger(trigger)
        print(f"{trigger['id']} {previous} -> {trigger['state']}")
        return 0
    raise AtlasError(f"subcomando trigger desconhecido: {args.trigger_command}")


def cmd_subscribe(store: Store, args) -> int:
    with store.transaction("subscription.subscribe") as tx:
        demand = _find_demand(tx.demands, args.demand_id)
        if demand.get("state") not in ACTIVE_STATES:
            raise AtlasError(
                f"demanda não está ativa: {args.demand_id} ({demand.get('state')})"
            )
        _find_thread(tx.threads, args.thread_id)
        existing = next(
            (
                row
                for row in tx.subscriptions
                if row.get("thread_id") == args.thread_id
                and row.get("demand_id") == args.demand_id
            ),
            None,
        )
        subscription = {
            "thread_id": args.thread_id,
            "demand_id": args.demand_id,
            "role": args.role,
            "active": True,
            "created_at": (existing or {}).get("created_at") or store.now_iso,
        }
        tx.put_subscription(subscription)
    print(f"{args.demand_id} subscribed {args.thread_id} role={args.role}")
    return 0


def cmd_unsubscribe(store: Store, args) -> int:
    with store.transaction("subscription.unsubscribe") as tx:
        existing = next(
            (
                row
                for row in tx.subscriptions
                if row.get("thread_id") == args.thread_id
                and row.get("demand_id") == args.demand_id
            ),
            None,
        )
        if existing is None:
            raise AtlasError(
                f"inscrição inexistente: {args.demand_id} em {args.thread_id}"
            )
        subscription = dict(existing)
        subscription["active"] = False
        tx.put_subscription(subscription)
    print(f"{args.demand_id} unsubscribed {args.thread_id}")
    return 0


def cmd_thread(store: Store, args) -> int:
    if args.thread_command != "list":
        raise AtlasError(f"subcomando thread desconhecido: {args.thread_command}")
    rows = store.threads()
    if args.open:
        rows = [thread for thread in rows if thread.get("state") == "open"]
    counts: dict[str, int] = {}
    for subscription in store.subscriptions():
        if subscription.get("active"):
            thread_id = subscription.get("thread_id")
            counts[thread_id] = counts.get(thread_id, 0) + 1
    rows.sort(
        key=lambda thread: (str(thread.get("last_trigger_at", "")), str(thread.get("id", ""))),
        reverse=True,
    )
    if args.limit >= 0:
        rows = rows[: args.limit]
    for thread in rows:
        print(
            f"{thread.get('id')} | {thread.get('state')} | {thread.get('source')} | "
            f"{thread.get('last_trigger_at')} | {counts.get(thread.get('id'), 0)} active | "
            f"{thread.get('title')}"
        )
    return 0


def _find_delivery(rows: list[dict], delivery_id: str) -> dict:
    for delivery in rows:
        if delivery.get("id") == delivery_id:
            return delivery
    raise AtlasError(f"delivery inexistente: {delivery_id}")


def cmd_delivery(store: Store, args) -> int:
    command = args.delivery_command

    if command == "list":
        rows = store.deliveries()
        if args.state:
            rows = [row for row in rows if row.get("state") in args.state]
        if args.demand:
            rows = [row for row in rows if row.get("demand_id") == args.demand]
        if args.trigger:
            rows = [row for row in rows if row.get("trigger_id") == args.trigger]
        rows.sort(
            key=lambda row: (str(row.get("created_at", "")), str(row.get("id", "")))
        )
        if args.limit >= 0:
            rows = rows[: args.limit]
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        for row in rows:
            print(deliveries.format_line(row))
        return 0

    if command == "expire":
        changed: list[str] = []
        with store.transaction("delivery.expire") as tx:
            for row in list(tx.deliveries):
                item = deliveries.expire(row, store.dt, store.now_iso)
                if item is None:
                    continue
                tx.put_delivery(item)
                changed.append(item["id"])
        print(f"expired={len(changed)}" + (f" ids={','.join(changed)}" if changed else ""))
        return 0

    with store.transaction(f"delivery.{command}") as tx:
        current = _find_delivery(tx.deliveries, args.delivery_id)
        if command == "claim":
            item = deliveries.claim(
                current, args.owner, args.lease_minutes, store.dt, store.now_iso
            )
            message = (
                f"{item['id']} delivered owner={item['lease_owner']} "
                f"epoch={item['lease_epoch']} lease_until={item['lease_until']}"
            )
        elif command == "ack":
            item = deliveries.ack(
                current, args.owner, args.epoch, args.report, store.now_iso, args.summary
            )
            message = f"{item['id']} acked report={item['report_id']}"
        elif command == "fail":
            item = deliveries.fail(
                current, args.error, store.now_iso, args.owner, args.epoch
            )
            message = f"{item['id']} failed attempts={item['attempts']}"
        elif command == "retry":
            item = deliveries.retry(current, store.now_iso)
            message = f"{item['id']} pending"
        else:
            raise AtlasError(f"subcomando delivery desconhecido: {command}")
        tx.put_delivery(item)
    print(message)
    return 0


def cmd_ingest(store: Store, args) -> int:
    if args.ingest_command != "email":
        raise AtlasError(f"subcomando ingest desconhecido: {args.ingest_command}")

    # Pausado: nem lê config nem abre conexão IMAP.
    control.assert_active(store, "reader")
    config = imap_reader.load_config()
    mailbox = args.mailbox or config["folder"]
    # A tentativa é registrada antes de abrir o socket: conexão/login que
    # falham também precisam aparecer no cursor e no health. `mark_*` relê o
    # cursor sob o lock e mexe só na telemetria.
    if not args.dry_run:
        imap_reader.mark_attempt(store, args.account, mailbox)
    try:
        client = imap_reader.RealImapClient(config)
    except AtlasError as exc:
        if not args.dry_run:
            imap_reader.mark_failure(store, args.account, mailbox, exc)
        raise
    with client:
        result = imap_reader.ingest_mailbox(
            store,
            client,
            account=args.account,
            mailbox=mailbox,
            limit=args.limit,
            since=args.since,
            unseen=args.unseen,
            dry_run=args.dry_run,
        )
    print(
        f"fetched={result['fetched']} created={result['created']} "
        f"duplicates={result['duplicates']} errors={result['errors']} "
        f"skipped={result['skipped']} "
        f"uidvalidity={result['uidvalidity']} last_uid={result['last_uid']} "
        f"shadow={result['shadow']}"
        + (" dry-run" if args.dry_run else "")
    )
    if result["warnings"]:
        print("warnings: " + ", ".join(result["warnings"]))
    if result["error"]:
        print(f"erro: {result['error']}", file=sys.stderr)
    # Erro de leitura não pode passar como sucesso para o daemon/cron.
    return 2 if result["errors"] > 0 else 0


def cmd_shadow(store: Store, args) -> int:
    if args.shadow_command != "report":
        raise AtlasError(f"subcomando shadow desconhecido: {args.shadow_command}")
    text = imap_reader.shadow_report(store, date=args.date)
    print(text, end="")
    return 0


def cmd_context(store: Store, args) -> int:
    if args.expand:
        print(context.expand(store, args.expand), end="")
        return 0
    pack = context.build(store, args.demand_id, budget=args.budget)
    if args.json:
        print(json.dumps(pack, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(context.render(pack), end="")
    return 0


DISPATCH = {
    "ingest": cmd_ingest,
    "shadow": cmd_shadow,
    "context": cmd_context,
    "trigger": cmd_trigger,
    "subscribe": cmd_subscribe,
    "unsubscribe": cmd_unsubscribe,
    "thread": cmd_thread,
    "delivery": cmd_delivery,
    "init": cmd_init,
    "add": cmd_add,
    "list": cmd_list,
    "show": cmd_show,
    "transition": cmd_transition,
    "classify": cmd_classify,
    "expire": cmd_expire,
    "events": cmd_events,
    "q": cmd_q,
    "rebuild": cmd_rebuild,
    "bulletin": cmd_bulletin,
    "digest": cmd_digest,
    "outbox": cmd_outbox,
    "kb": cmd_kb,
    "migrate": cmd_migrate,
    "doctor": cmd_doctor,
    "pause": cmd_pause,
    "resume": cmd_resume,
    "health": cmd_health,
    "backup": cmd_backup,
    "watchdog": cmd_watchdog,
}


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    _now, rest = daemon.split_now(raw)
    if rest[:1] == ["daemon"]:
        try:
            return daemon.command(rest[1:])
        except AtlasError as exc:
            print(f"atlas: {exc}", file=sys.stderr)
            return exc.code
    forwarded = daemon.forward(raw)
    if forwarded is not None:
        return forwarded
    parser = build_parser()
    args = parser.parse_args(raw)
    try:
        store = Store(now=getattr(args, "now", None))
        store.init()
        store.recover()
        return DISPATCH[args.command](store, args)
    except AtlasError as exc:
        print(f"atlas: {exc}", file=sys.stderr)
        return exc.code
    except BrokenPipeError:
        return 0
