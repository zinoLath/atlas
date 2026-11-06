"""Renderização de frontmatter YAML e markdown (vista derivada)."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .util import write_text

_PLAIN_RE = re.compile(r"[A-Za-z0-9_./@+\-]+")
_RESERVED = {"null", "true", "false", "yes", "no", "on", "off", "~"}


def _scalar(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value) if isinstance(value, float) else str(value)
    text = str(value)
    if text == "":
        return '""'
    if _PLAIN_RE.fullmatch(text) and text.lower() not in _RESERVED:
        return text
    return json.dumps(text, ensure_ascii=False)


def yaml_dump(mapping: dict) -> str:
    lines: list[str] = []

    def dump_dict(obj: dict, indent: int) -> None:
        pad = " " * indent
        for key, value in obj.items():
            if isinstance(value, dict):
                if value:
                    lines.append(f"{pad}{key}:")
                    dump_dict(value, indent + 2)
                else:
                    lines.append(f"{pad}{key}: {{}}")
            elif isinstance(value, list):
                if value:
                    lines.append(f"{pad}{key}:")
                    dump_list(value, indent + 2)
                else:
                    lines.append(f"{pad}{key}: []")
            else:
                lines.append(f"{pad}{key}: {_scalar(value)}")

    def dump_list(items: list, indent: int) -> None:
        pad = " " * indent
        for item in items:
            if isinstance(item, dict):
                lines.append(f"{pad}- {json.dumps(item, ensure_ascii=False, sort_keys=True)}")
            else:
                lines.append(f"{pad}- {_scalar(item)}")

    dump_dict(mapping, 0)
    return "\n".join(lines) + "\n"


def split_frontmatter(text: str) -> str:
    """Devolve o corpo após o segundo delimitador `---`."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return text
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return "\n".join(lines[index + 1 :]).lstrip("\n")
    return ""


def read_body(path: Path) -> str:
    if not path.exists():
        return ""
    return split_frontmatter(path.read_text(encoding="utf-8"))


def compose(frontmatter: dict, body: str) -> str:
    body = body.rstrip()
    return f"---\n{yaml_dump(frontmatter)}---\n\n{body}\n"


DEMAND_FRONTMATTER_KEYS = (
    "id",
    "area",
    "title",
    "state",
    "created_at",
    "updated_at",
    "source",
    "links",
    "due",
    "estimate_min",
    "classification",
    "lane",
    "next_action",
    "owner",
    "lease_until",
    "lease_epoch",
    "wake_at",
    "artifact",
    "waiting_on",
    "follow_up_at",
    "rollover",
    "result",
    "drop_reason",
)

KB_FRONTMATTER_KEYS = (
    "id",
    "kind",
    "title",
    "summary",
    "tags",
    "importance",
    "parent",
    "created_at",
    "updated_at",
)


def _ordered(record: dict, keys: tuple[str, ...]) -> dict:
    ordered = {key: record.get(key) for key in keys if key in record}
    for key in sorted(record):
        if key not in ordered:
            ordered[key] = record[key]
    return ordered


def render_demand(path: Path, demand: dict) -> None:
    body = read_body(path) or f"# {demand['title']}\n\n## Notas\n"
    write_text(path, compose(_ordered(demand, DEMAND_FRONTMATTER_KEYS), body))


def render_kb(path: Path, node: dict) -> None:
    body = read_body(path) or f"# {node['title']}\n\n{node.get('summary', '')}\n"
    write_text(path, compose(_ordered(node, KB_FRONTMATTER_KEYS), body))


def write_initial_body(path: Path, text: str) -> None:
    write_text(path, text if text.endswith("\n") else text + "\n")
