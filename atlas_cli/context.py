"""Context pack: IDs, resumos e ponteiros para o Worker (M1b.0b).

Somente leitura: nada é escrito no estado canônico, nenhum modelo é chamado.
O Worker recebe um pacote pequeno (default 8 KB) e expande sob demanda com
`atlas context <dmd> --expand <id>`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import kb
from .states import ACTIVE_STATES
from .store import Store
from .util import AtlasError, collapse_ws

DEFAULT_BUDGET = 8000
KB_LIMIT = 5
KB_MIN = 3
TRIGGER_LIMIT = 3
SIBLING_LIMIT = 3
SUMMARY_LIMIT = 200
EXPAND_LIMIT = 20000

# Linhas de credencial viram `***`; segredos nunca entram no pack.
_SECRET_LINE = re.compile(
    r"(?im)^[A-Z0-9_]*(?:PASS|PASSWD|TOKEN|SECRET|API_KEY)[A-Z0-9_]*\s*[=:]\s*\S+$"
)


def _redact(text: str) -> str:
    return _SECRET_LINE.sub("***", text)


def _clean(text) -> str:
    return _redact(collapse_ws(str(text or "")))


def _shorten(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def _find(rows: list[dict], object_id: str, what: str) -> dict:
    for row in rows:
        if row.get("id") == object_id:
            return row
    raise AtlasError(f"{what} inexistente: {object_id}")


def _threads(store: Store, demand: dict) -> list[dict]:
    demand_id = demand["id"]
    subscribed = {
        row.get("thread_id")
        for row in store.subscriptions()
        if row.get("demand_id") == demand_id and row.get("active")
    }
    triggers = store.triggers()
    items: list[dict] = []
    for thread in store.threads():
        thread_id = thread.get("id")
        if thread_id not in subscribed:
            continue
        rows = [trigger for trigger in triggers if trigger.get("thread_id") == thread_id]
        rows.sort(
            key=lambda trigger: (
                str(trigger.get("received_at") or ""),
                str(trigger.get("id") or ""),
            )
        )
        items.append(
            {
                "id": thread_id,
                "title": _clean(thread.get("title")),
                "last_trigger_at": thread.get("last_trigger_at"),
                "triggers": [
                    {
                        "id": trigger.get("id"),
                        "actor": trigger.get("actor") or "",
                        "title": _clean(trigger.get("title")),
                        "received_at": trigger.get("received_at"),
                        "body_path": trigger.get("body_path") or "",
                    }
                    for trigger in rows[-TRIGGER_LIMIT:]
                ],
            }
        )
    items.sort(
        key=lambda item: (str(item.get("last_trigger_at") or ""), str(item["id"])),
        reverse=True,
    )
    return items


def _expand_command(demand_id: str, object_id: str) -> str:
    return f"atlas context {demand_id} --expand {object_id}"


def _kb_item(demand_id: str, node: dict, relation: str) -> dict:
    summary = _clean(node.get("summary"))
    return {
        "id": node["id"],
        "title": _clean(node.get("title")),
        "summary": _shorten(summary, SUMMARY_LIMIT),
        "relation": relation,
        "expand": _expand_command(demand_id, node["id"]),
    }


def _kb_items(store: Store, demand: dict) -> list[dict]:
    demand_id = demand["id"]
    nodes = kb.nodes_by_id(store)
    items: list[dict] = []
    seen: set[str] = set()

    for node_id in (demand.get("links") or {}).get("kb") or []:
        node = nodes.get(node_id)
        if node is None or node_id in seen:
            continue
        seen.add(node_id)
        items.append(_kb_item(demand_id, node, "link"))

    if len(items) < KB_LIMIT:
        area = demand.get("area") or None
        for hit in kb.search(store, demand.get("title") or "", area=area, limit=KB_LIMIT):
            node_id = hit.get("id")
            if node_id in seen or node_id not in nodes:
                continue
            seen.add(node_id)
            why = set((hit.get("why") or "").split(","))
            items.append(
                _kb_item(demand_id, nodes[node_id], "fts" if "fts" in why else "edge")
            )
            if len(items) >= KB_LIMIT:
                break

    return items[:KB_LIMIT]


def _siblings(store: Store, demand: dict) -> list[dict]:
    rows = [
        other
        for other in store.demands()
        if other.get("area") == demand.get("area")
        and other.get("id") != demand["id"]
        and other.get("state") in ACTIVE_STATES
    ]
    rows.sort(
        key=lambda row: (str(row.get("updated_at") or ""), str(row.get("id") or "")),
        reverse=True,
    )
    rows.sort(key=lambda row: (row.get("due") is None, str(row.get("due") or "")))
    return [
        {
            "id": row["id"],
            "title": _clean(row.get("title")),
            "state": row.get("state") or "",
            "due": row.get("due"),
        }
        for row in rows[:SIBLING_LIMIT]
    ]


def _spill(
    store: Store, demand: dict, threads: list[dict], kb_items: list[dict]
) -> list[dict]:
    items: list[dict] = []
    for thread in threads:
        for trigger in thread["triggers"]:
            body_path = trigger.get("body_path")
            if body_path and (store.home / body_path).exists():
                items.append(
                    {
                        "kind": "trigger",
                        "id": trigger["id"],
                        "path": body_path,
                        "hint": "corpo do trigger",
                    }
                )
    for item in kb_items:
        path = f"kb/{item['id']}.md"
        if (store.home / path).exists():
            items.append(
                {"kind": "kb", "id": item["id"], "path": path, "hint": "nó completo"}
            )
    demand_path = f"demands/{demand['id']}.md"
    if (store.home / demand_path).exists():
        items.append(
            {
                "kind": "demand",
                "id": demand["id"],
                "path": demand_path,
                "hint": "demanda completa",
            }
        )
    return items


def build(store: Store, demand_id: str, *, budget: int = DEFAULT_BUDGET) -> dict:
    """Monta o pacote determinístico da demanda, já ajustado ao orçamento."""
    if budget <= 0:
        raise AtlasError("--budget deve ser positivo")
    demand = _find(store.demands(), demand_id, "demanda")
    threads = _threads(store, demand)
    kb_items = _kb_items(store, demand)
    pack = {
        "demand": {
            "id": demand["id"],
            "title": _clean(demand.get("title")),
            "state": demand.get("state") or "",
            "area": demand.get("area") or "",
            "lane": demand.get("lane"),
            "due": demand.get("due"),
            "thread_ids": [thread["id"] for thread in threads],
        },
        "threads": threads,
        "kb": kb_items,
        "siblings": _siblings(store, demand),
        "spill": _spill(store, demand, threads, kb_items),
        "budget": {"limit": budget, "used": 0, "truncated": False, "omitted": []},
    }
    _fit(pack, budget)
    return pack


def _over(pack: dict, budget: int) -> bool:
    return len(render(pack)) > budget


def _condense(pack: dict, title_limit: int, summary_limit: int) -> bool:
    changed = False
    for thread in pack["threads"]:
        for row, limit in [(thread, title_limit)] + [
            (trigger, title_limit) for trigger in thread["triggers"]
        ]:
            shortened = _shorten(row["title"], limit)
            if shortened != row["title"]:
                row["title"] = shortened
                changed = True
    for item in pack["kb"]:
        for key, limit in (("title", title_limit), ("summary", summary_limit)):
            shortened = _shorten(item[key], limit)
            if shortened != item[key]:
                item[key] = shortened
                changed = True
    return changed


def _fit(pack: dict, budget: int) -> None:
    """Aplica o corte na ordem do briefing: irmãs, KB, títulos/trechos."""
    box = pack["budget"]
    box["limit"] = budget
    box["used"] = 0
    box["truncated"] = False
    box["omitted"] = []
    omitted = box["omitted"]

    if not _over(pack, budget):
        box["used"] = len(render(pack))
        return

    box["truncated"] = True
    if pack["siblings"]:
        count = len(pack["siblings"])
        pack["siblings"] = []
        omitted.append(f"{count} irmã(s)")

    if _over(pack, budget):
        dropped = max(0, len(pack["kb"]) - KB_MIN)
        if dropped:
            pack["kb"] = pack["kb"][:KB_MIN]
            omitted.append(f"{dropped} nó(s) da KB")

    for title_limit, summary_limit in ((60, 120), (40, 60), (24, 40)):
        if not _over(pack, budget):
            break
        if _condense(pack, title_limit, summary_limit):
            omitted.append(f"títulos/trechos com {title_limit}/{summary_limit} chars")

    if not omitted:
        omitted.append("orçamento excedido")
    box["used"] = len(render(pack))


def _due(value) -> str:
    return str(value) if value else "-"


def render(pack: dict) -> str:
    """Markdown legível do pacote (mesma entrada → mesmo output byte a byte)."""
    demand = pack["demand"]
    lines: list[str] = [
        f"# CONTEXT {demand['id']}",
        "",
        "## objeto",
        f"- {demand['id']} — {demand['title']}",
        (
            f"- estado: {demand['state']} | área: {demand['area']} | "
            f"lane: {demand['lane'] or '-'} | due: {_due(demand['due'])}"
        ),
        "- threads: " + (", ".join(demand["thread_ids"]) or "-"),
        "",
        "## thread",
    ]
    if not pack["threads"]:
        lines.append("- (nenhuma)")
    for thread in pack["threads"]:
        lines.append(
            f"- {thread['id']} ({len(thread['triggers'])} triggers) {thread['title']}"
        )
        for trigger in thread["triggers"]:
            lines.append(
                f"  - {trigger['id']} — {trigger['actor'] or '-'} — {trigger['title']} "
                f"({trigger['received_at']}) — {trigger['body_path'] or '-'}"
            )

    lines.extend(["", "## kb"])
    if not pack["kb"]:
        lines.append("- (nenhum)")
    for item in pack["kb"]:
        lines.append(
            f"- `{item['id']}` — {item['summary'] or '-'} — {item['relation']} "
            f"— expandir: {item['expand']}"
        )

    lines.extend(["", "## irmãs"])
    if not pack["siblings"]:
        lines.append("- (nenhuma)")
    for item in pack["siblings"]:
        lines.append(
            f"- {item['id']} — {item['state']} — due {_due(item['due'])} — {item['title']}"
        )

    lines.extend(["", "## spill"])
    if not pack["spill"]:
        lines.append("- (nada)")
    for item in pack["spill"]:
        lines.append(f"- {item['path']} ({item['hint']})")

    if pack["budget"]["truncated"]:
        omit = "; ".join(pack["budget"]["omitted"]) or "orçamento excedido"
        lines.extend(
            ["", f"> truncado: {omit} (orçamento {pack['budget']['limit']} chars)"]
        )

    return "\n".join(lines) + "\n"


def _clip(text: str, pointer: Path | None) -> str:
    data = text.encode("utf-8")
    if len(data) <= EXPAND_LIMIT:
        return text
    where = f" veja {pointer}" if pointer is not None else ""
    notice = f"\n... [truncado em {EXPAND_LIMIT} bytes;{where}]\n"
    room = EXPAND_LIMIT - len(notice.encode("utf-8"))
    clipped = data[:room].decode("utf-8", "ignore")
    return clipped.rstrip() + notice


def _read_text(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AtlasError(f"arquivo ilegível: {path} ({exc})") from exc


def _expand_trigger(store: Store, object_id: str) -> str:
    trigger = _find(store.triggers(), object_id, "trigger")
    body_path = trigger.get("body_path") or ""
    path = store.home / body_path if body_path else None
    body = _read_text(path) if path is not None else ""
    lines = [
        f"# {trigger['id']}",
        f"- thread: {trigger.get('thread_id')}",
        f"- source: {trigger.get('source')} | external: {trigger.get('external_id')}",
        (
            f"- recebido: {trigger.get('received_at')} | "
            f"ator: {trigger.get('actor') or '-'} | estado: {trigger.get('state')}"
        ),
        f"- título: {trigger.get('title')}",
        f"- corpo: {body_path or '(sem corpo)'}",
        "",
        body.rstrip("\n"),
    ]
    return _clip(_redact("\n".join(lines).rstrip() + "\n"), path)


def _expand_thread(store: Store, object_id: str) -> str:
    thread = _find(store.threads(), object_id, "thread")
    rows = [
        trigger
        for trigger in store.triggers()
        if trigger.get("thread_id") == object_id
    ]
    rows.sort(
        key=lambda trigger: (
            str(trigger.get("received_at") or ""),
            str(trigger.get("id") or ""),
        )
    )
    lines = [
        f"# {thread['id']}",
        f"- source: {thread.get('source')} | external: {thread.get('external_id')}",
        (
            f"- estado: {thread.get('state')} | último: {thread.get('last_trigger_at')} | "
            f"triggers: {len(rows)}"
        ),
        f"- título: {thread.get('title')}",
        "",
    ]
    for trigger in rows:
        lines.append(
            f"- {trigger.get('id')} — {trigger.get('received_at')} — "
            f"{trigger.get('actor') or '-'} — {trigger.get('title')}"
        )
    return _clip(_redact("\n".join(lines).rstrip() + "\n"), None)


def _expand_demand(store: Store, object_id: str) -> str:
    demand = _find(store.demands(), object_id, "demanda")
    text = json.dumps(demand, ensure_ascii=False, indent=2, sort_keys=True)
    path = store.demand_md(object_id)
    if path.exists():
        text += f"\n\n## demands/{object_id}.md\n\n" + _read_text(path).rstrip("\n")
    return _clip(_redact(text + "\n"), path)


def _expand_kb(store: Store, object_id: str) -> str:
    node = kb.nodes_by_id(store).get(object_id)
    if node is None:
        raise AtlasError(f"objeto inexistente: {object_id}")
    path = store.kb_md(object_id)
    if path.exists():
        return _clip(_redact(_read_text(path)), path)
    text = json.dumps(node, ensure_ascii=False, indent=2, sort_keys=True)
    return _clip(_redact(text + "\n"), None)


def expand(store: Store, object_id: str) -> str:
    """Expande um ID do pacote (trigger, thread, demanda ou nó de KB)."""
    oid = (object_id or "").strip()
    if not oid:
        raise AtlasError("id de expansão vazio")
    if oid.startswith("trg-"):
        return _expand_trigger(store, oid)
    if oid.startswith("thr-"):
        return _expand_thread(store, oid)
    if oid.startswith("dmd-"):
        return _expand_demand(store, oid)
    return _expand_kb(store, oid)
