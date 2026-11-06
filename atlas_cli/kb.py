"""Base de conhecimento em árvore (id = caminho) com arestas extras e FTS5."""

from __future__ import annotations

import re

from . import policy, render
from .store import Store
from .util import (
    AtlasError,
    collapse_ws,
    jaccard,
    slugify,
    title_tokens,
    write_text,
)

REL_DEFAULT_WEIGHT = 0.5
MIN_EDGE_WEIGHT = 0.3
INDEX_SUMMARY_LIMIT = 120


def nodes_by_id(store: Store) -> dict[str, dict]:
    return {node["id"]: node for node in store.kb_nodes()}


def _require_node(nodes: dict[str, dict], node_id: str) -> dict:
    if node_id not in nodes:
        raise AtlasError(f"nó inexistente: {node_id!r}")
    return nodes[node_id]


def add_node(
    tx,
    *,
    parent: str,
    kind: str,
    title: str,
    summary: str,
    tags: list[str] | None = None,
    importance: int = 3,
) -> dict:
    title = title.strip()
    if not title:
        raise AtlasError("título vazio")
    if not kind.strip():
        raise AtlasError("kind vazio")
    if not 1 <= int(importance) <= 5:
        raise AtlasError("importance deve estar entre 1 e 5")

    parent = (parent or "").strip()
    slug = slugify(title)
    if not slug:
        raise AtlasError(f"título sem slug utilizável: {title!r}")

    nodes = {node["id"]: node for node in tx.kb_nodes}
    if parent:
        _require_node(nodes, parent)
        node_id = f"{parent}/{slug}"
    else:
        if slug not in policy.AREAS:
            raise AtlasError(
                f"raiz de área deve ser uma de {', '.join(policy.AREAS)}; recebido {slug!r}"
            )
        node_id = slug

    if node_id in nodes:
        raise AtlasError(f"nó já existe: {node_id}")

    node = {
        "id": node_id,
        "kind": kind.strip(),
        "title": title,
        "summary": summary or "",
        "tags": list(tags or []),
        "importance": int(importance),
        "parent": parent,
        "created_at": tx.store.now_iso,
        "updated_at": tx.store.now_iso,
    }
    return tx.upsert_kb(node)


def link(
    tx,
    src: str,
    rel: str,
    dst: str,
    *,
    weight: float = REL_DEFAULT_WEIGHT,
    note: str = "",
) -> dict:
    rel = rel.strip()
    if not rel:
        raise AtlasError("relação vazia")
    if src == dst:
        raise AtlasError(f"self-edge não permitido: {src}")
    nodes = {node["id"]: node for node in tx.kb_nodes}
    _require_node(nodes, src)
    _require_node(nodes, dst)
    for edge in tx.edges:
        if edge.get("src") == src and edge.get("dst") == dst and edge.get("rel") == rel:
            raise AtlasError(f"aresta duplicada: {src} -{rel}-> {dst}")
    edge = {
        "src": src,
        "rel": rel,
        "dst": dst,
        "weight": float(weight),
        "note": note or "",
        "created_at": tx.store.now_iso,
    }
    return tx.upsert_edge(edge)


def tree(store: Store, node_id: str) -> str:
    nodes = nodes_by_id(store)
    _require_node(nodes, node_id)
    children: dict[str, list[str]] = {}
    for other_id, node in nodes.items():
        children.setdefault(node.get("parent", ""), []).append(other_id)
    for values in children.values():
        values.sort()

    lines: list[str] = []

    def walk(current: str, depth: int) -> None:
        segment = current.rsplit("/", 1)[-1]
        lines.append(f"{'  ' * depth}{segment}  {nodes[current]['title']}")
        for child in children.get(current, []):
            walk(child, depth + 1)

    walk(node_id, 0)
    return "\n".join(lines)


def path(store: Store, node_id: str) -> str:
    nodes = nodes_by_id(store)
    _require_node(nodes, node_id)
    parts = node_id.split("/")
    lines = ["/".join(parts[: index + 1]) for index in range(len(parts))]
    return "\n".join(lines)


def related(store: Store, node_id: str) -> str:
    nodes = nodes_by_id(store)
    _require_node(nodes, node_id)
    out_edges = [edge for edge in store.edges() if edge.get("src") == node_id]
    in_edges = [edge for edge in store.edges() if edge.get("dst") == node_id]

    lines: list[str] = ["OUT"]
    grouped: dict[str, list[dict]] = {}
    for edge in out_edges:
        grouped.setdefault(edge["rel"], []).append(edge)
    for rel in sorted(grouped):
        lines.append(f"  {rel}:")
        for edge in sorted(grouped[rel], key=lambda item: item["dst"]):
            note = f" — {edge['note']}" if edge.get("note") else ""
            lines.append(f"    - {edge['dst']} (peso {float(edge['weight']):.2f}){note}")
    if not grouped:
        lines.append("  (nenhuma)")

    lines.append("IN")
    grouped = {}
    for edge in in_edges:
        grouped.setdefault(edge["rel"], []).append(edge)
    for rel in sorted(grouped):
        lines.append(f"  {rel}:")
        for edge in sorted(grouped[rel], key=lambda item: item["src"]):
            note = f" — {edge['note']}" if edge.get("note") else ""
            lines.append(f"    - {edge['src']} (peso {float(edge['weight']):.2f}){note}")
    if not grouped:
        lines.append("  (nenhuma)")
    return "\n".join(lines)


def check(store: Store) -> list[str]:
    problems: list[str] = []
    raw_nodes = store.kb_nodes()
    seen: set[str] = set()
    for node in raw_nodes:
        node_id = node.get("id", "")
        if node_id in seen:
            problems.append(f"id duplicado: {node_id}")
        seen.add(node_id)

    nodes = {node.get("id", ""): node for node in raw_nodes}
    for node_id, node in nodes.items():
        parent = node.get("parent", "") or ""
        if not node.get("summary"):
            problems.append(f"stub sem summary: {node_id}")
        if not store.kb_md(node_id).exists():
            problems.append(f"arquivo ausente: {node_id}")
        if not node.get("title"):
            problems.append(f"nó sem título: {node_id!r}")
        if "/" in node_id:
            expected_parent = node_id.rsplit("/", 1)[0]
            if parent != expected_parent:
                problems.append(
                    f"pai não é prefixo do id: {node_id} (parent={parent!r})"
                )
        elif parent:
            problems.append(f"raiz com parent não vazio: {node_id} (parent={parent!r})")
        if parent:
            if parent not in nodes:
                problems.append(f"pai inexistente: {node_id} (parent={parent!r})")
            elif not node_id.startswith(parent + "/"):
                problems.append(f"id fora do caminho do pai: {node_id}")
        else:
            if node_id not in policy.AREAS:
                problems.append(f"raiz fora das áreas: {node_id}")

    seen_edges: set[tuple[str, str, str]] = set()
    for edge in store.edges():
        src = edge.get("src", "")
        dst = edge.get("dst", "")
        rel = edge.get("rel", "")
        if src == dst:
            problems.append(f"self-edge: {src} -{rel}-> {dst}")
        if src not in nodes:
            problems.append(f"aresta para origem inexistente: {src} -{rel}-> {dst}")
        if dst not in nodes:
            problems.append(f"aresta para destino inexistente: {src} -{rel}-> {dst}")
        key = (src, dst, rel)
        if key in seen_edges:
            problems.append(f"aresta duplicada: {src} -{rel}-> {dst}")
        seen_edges.add(key)

    return problems


def _short_summary(text: str) -> str:
    text = collapse_ws(text or "")
    if len(text) > INDEX_SUMMARY_LIMIT:
        return text[:INDEX_SUMMARY_LIMIT].rstrip() + "…"
    return text


def build_index(store: Store) -> str:
    """Monta o INDEX.md navegável: seções por área, árvore indentada por id."""
    nodes = nodes_by_id(store)
    children: dict[str, list[str]] = {}
    for node_id, node in nodes.items():
        parent = node.get("parent", "") or ""
        children.setdefault(parent, []).append(node_id)
    for values in children.values():
        values.sort()

    def render_line(node_id: str, depth: int) -> str:
        node = nodes[node_id]
        line = f"{'  ' * depth}- [{node.get('title', '')}](kb/{node_id}.md)"
        summary = _short_summary(node.get("summary", ""))
        return f"{line} — {summary}" if summary else line

    lines: list[str] = [
        "# ÍNDICE — base de conhecimento",
        "",
        f"Gerado em {store.now_iso}. {len(nodes)} nós, {len(store.edges())} arestas.",
    ]

    def walk(node_id: str, depth: int) -> None:
        lines.append(render_line(node_id, depth))
        for child in children.get(node_id, []):
            walk(child, depth + 1)

    for area in policy.AREAS:
        if area not in nodes:
            continue
        count = sum(
            1
            for node_id in nodes
            if node_id == area or node_id.startswith(area + "/")
        )
        lines.extend(["", f"## {area} ({count} nós)"])
        walk(area, 0)

    return "\n".join(lines) + "\n"


def index(store: Store) -> int:
    # SQLite e INDEX.md saem do mesmo snapshot: `rebuild_db` dentro do lock
    # reaproveita o depth e o relatório nunca descreve uma árvore mais nova
    # que o próprio hub.db.
    with store.lock():
        store.rebuild_db()
        nodes = store.kb_nodes()
        write_text(store.home / "INDEX.md", build_index(store))
    return len(nodes)


def dup(store: Store) -> list[tuple[str, str, float]]:
    nodes = store.kb_nodes()
    tokens = {node["id"]: title_tokens(node.get("title", "")) for node in nodes}
    results: list[tuple[str, str, float]] = []
    ids = sorted(tokens)
    for i, left in enumerate(ids):
        for right in ids[i + 1 :]:
            similarity = jaccard(tokens[left], tokens[right])
            if similarity > 0.8:
                results.append((left, right, similarity))
    return results


def _snippet(store: Store, node_id: str, limit: int = 200) -> str:
    body = render.read_body(store.kb_md(node_id))
    body = " ".join(line.lstrip("# ").strip() for line in body.splitlines())
    body = collapse_ws(body)
    if not body:
        node = nodes_by_id(store).get(node_id, {})
        body = collapse_ws(node.get("summary", "") or "")
    return body[:limit]


def search(
    store: Store,
    query: str,
    *,
    area: str | None = None,
    limit: int = 5,
) -> list[dict]:
    store.ensure_db()
    tokens = re.findall(r"\w+", query.lower())
    if not tokens:
        return []
    match_expr = " OR ".join(f'"{token}"*' for token in tokens)
    sql = (
        "SELECT node_id, bm25(kb_fts) AS rank FROM kb_fts "
        "WHERE kb_fts MATCH ?"
    )
    params: list = [match_expr]
    if area:
        sql += " AND (node_id = ? OR node_id LIKE ?)"
        params.extend([area, f"{area}/%"])
    sql += " ORDER BY rank LIMIT 20"

    conn = store.connect()
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    nodes = nodes_by_id(store)
    candidates: dict[str, dict] = {}

    def offer(node_id: str, score: float, why: str) -> None:
        current = candidates.get(node_id)
        if current is None:
            candidates[node_id] = {"score": score, "why": {why}}
        else:
            current["why"].add(why)
            if score > current["score"]:
                current["score"] = score

    fts_ranks: dict[str, float] = {}
    for row in rows:
        node_id = row["node_id"]
        if node_id not in nodes:
            continue
        rank = float(row["rank"])
        fts_ranks[node_id] = rank
        importance = int(nodes[node_id].get("importance", 3))
        offer(node_id, -rank + 0.1 * importance, "fts")

    edges = store.edges()
    for source in list(fts_ranks):
        for edge in edges:
            if edge.get("src") != source and edge.get("dst") != source:
                continue
            if float(edge.get("weight", 0.0)) < MIN_EDGE_WEIGHT:
                continue
            target = edge["dst"] if edge["src"] == source else edge["src"]
            if target == source or target not in nodes:
                continue
            if target in fts_ranks:
                continue
            importance = int(nodes[target].get("importance", 3))
            # Vizinhos não têm bm25 próprio: bm25 = 0.
            score = 0.5 * float(edge.get("weight", 0.0)) + 0.1 * importance
            offer(target, score, f"edge:{edge['rel']}")

    for source in list(fts_ranks):
        parent = nodes[source].get("parent") or ""
        if parent and parent in nodes:
            offer(parent, 0.1 * int(nodes[parent].get("importance", 3)), "parent")

    ranked = sorted(
        candidates.items(),
        key=lambda item: (
            0 if "fts" in item[1]["why"] else 1,
            -item[1]["score"],
            item[0],
        ),
    )[:limit]

    results: list[dict] = []
    for node_id, data in ranked:
        results.append(
            {
                "id": node_id,
                "title": nodes[node_id].get("title", ""),
                "snippet": _snippet(store, node_id),
                "why": ",".join(sorted(data["why"])),
                "score": data["score"],
            }
        )
    return results
