"""Triggers, threads e subscriptions: máquina de estados e fast path (M1b.0a).

O fast path é determinístico: consulta a subscription ativa da thread, senão
aplica `router/rules.v1.json` na ordem do arquivo. Nenhuma chamada de modelo
acontece aqui.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import policy
from .util import AtlasError

TRIGGER_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "new": ("candidate", "ignored", "routed"),
    "candidate": ("routed", "ignored", "needs_human"),
    "routed": ("processed", "skipped", "needs_human"),
    "ignored": (),
    "processed": (),
    "skipped": (),
    "needs_human": (),
}

TRIGGER_STATES = frozenset(TRIGGER_TRANSITIONS)

DEFAULT_RULES: dict = {
    "version": 1,
    "default": "candidate",
    "rules": [
        {
            "name": "projetos-fapemig",
            "match": {
                "source": "email",
                "subject_regex": "(?i)(fapemig|wsf|paper|artigo)",
            },
            "action": "route",
            "area": "projetos",
        },
        {
            "name": "faculdade-sigaa",
            "match": {"source": "email", "from_regex": "(?i)sigaa"},
            "action": "route",
            "area": "faculdade",
        },
        {
            "name": "financas-nubank",
            "match": {
                "source": "email",
                "subject_regex": "(?i)(fatura|nubank|pagamento)",
            },
            "action": "route",
            "area": "financas",
        },
    ],
}

MATCH_KEYS = frozenset(
    {"source", "kind", "from_regex", "subject_regex", "labels_any", "thread_key_regex"}
)
ACTIONS = frozenset({"ignore", "route"})
DEFAULT_PRIORITY = 100


def rules_path(home: Path) -> Path:
    return home / "router" / "rules.v1.json"


def blob_relpath(sha: str) -> str:
    """Caminho content-addressed do corpo bruto de um trigger."""
    return f"raw/blobs/{sha[:2]}/{sha}.txt"


def _raise_rule(name: str, message: str) -> AtlasError:
    return AtlasError(f"regra {name!r}: {message}")


def _string_list(value, name: str, field: str) -> list[str]:
    items = value if isinstance(value, list) else [value]
    if not items or any(not isinstance(item, str) for item in items):
        raise _raise_rule(name, f"{field} deve ser string ou lista de strings")
    return list(items)


def _regex(value, name: str, field: str) -> re.Pattern:
    if not isinstance(value, str):
        raise _raise_rule(name, f"{field} deve ser string")
    try:
        return re.compile(value)
    except re.error as exc:
        raise _raise_rule(name, f"{field} inválida: {exc}") from exc


def _compile_match(raw, name: str) -> dict:
    if not isinstance(raw, dict):
        raise _raise_rule(name, "match deve ser objeto")
    unknown = set(raw) - MATCH_KEYS
    if unknown:
        raise _raise_rule(name, f"match com chave desconhecida: {', '.join(sorted(unknown))}")
    match: dict = {}
    for key in ("source", "kind"):
        if key in raw:
            match[key] = _string_list(raw[key], name, key)
    for key in ("from_regex", "subject_regex"):
        if key in raw:
            match[key] = _regex(raw[key], name, key)
    if "thread_key_regex" in raw:
        match["thread_key_regex"] = _regex(raw["thread_key_regex"], name, "thread_key_regex")
    if "labels_any" in raw:
        match["labels_any"] = _string_list(raw["labels_any"], name, "labels_any")
    return match


def _priority(raw, name: str) -> int:
    value = raw.get("priority", DEFAULT_PRIORITY)
    if isinstance(value, bool) or not isinstance(value, int):
        raise _raise_rule(name, "priority deve ser inteiro")
    return value


def _normalize(doc, path: Path) -> dict:
    if not isinstance(doc, dict):
        raise AtlasError(f"regras inválidas em {path}: esperado objeto JSON")
    if doc.get("version") != 1:
        raise AtlasError(f"regras inválidas em {path}: versão {doc.get('version')!r} não suportada")
    default = doc.get("default", "candidate")
    if default not in ("candidate", "ignore"):
        raise AtlasError(f"regras inválidas em {path}: default deve ser candidate ou ignore")
    raw_rules = doc.get("rules", [])
    if not isinstance(raw_rules, list):
        raise AtlasError(f"regras inválidas em {path}: rules deve ser lista")

    rules: list[dict] = []
    for index, raw in enumerate(raw_rules):
        if not isinstance(raw, dict):
            raise AtlasError(f"regras inválidas em {path}: regra #{index + 1} não é objeto")
        name = raw.get("name") or f"rule-{index + 1}"
        if not isinstance(name, str) or not name:
            raise AtlasError(f"regras inválidas em {path}: regra #{index + 1} sem nome")
        action = raw.get("action")
        if action not in ACTIONS:
            raise _raise_rule(name, f"action desconhecida: {action!r}")
        area = raw.get("area")
        if action == "route" and area not in policy.AREAS:
            raise _raise_rule(
                name,
                f"área inválida: {area!r} (use {', '.join(policy.AREAS)})",
            )
        rules.append(
            {
                "name": name,
                "action": action,
                "area": area if action == "route" else None,
                "match": _compile_match(raw.get("match", {}), name),
                "priority": _priority(raw, name),
                "order": index,
            }
        )
    rules.sort(key=lambda rule: (rule["priority"], rule["order"]))
    return {"default": default, "rules": rules}


def load_rules(home: Path) -> dict:
    path = rules_path(home)
    if not path.exists():
        return _normalize(DEFAULT_RULES, path)
    try:
        with path.open("r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except json.JSONDecodeError as exc:
        raise AtlasError(f"regras inválidas em {path}: {exc.msg}") from exc
    return _normalize(doc, path)


def matches(rule: dict, trigger: dict) -> bool:
    match = rule["match"]
    if "source" in match and trigger.get("source") not in match["source"]:
        return False
    if "kind" in match and trigger.get("kind") not in match["kind"]:
        return False
    if "from_regex" in match and not match["from_regex"].search(trigger.get("actor") or ""):
        return False
    if "subject_regex" in match and not match["subject_regex"].search(
        trigger.get("title") or ""
    ):
        return False
    if "thread_key_regex" in match and not match["thread_key_regex"].search(
        trigger.get("thread_key") or ""
    ):
        return False
    if "labels_any" in match:
        labels = set((trigger.get("meta") or {}).get("labels") or [])
        if not labels & set(match["labels_any"]):
            return False
    return True


def empty_route() -> dict:
    return {
        "by": None,
        "area": None,
        "demand_id": None,
        "demand_ids": [],
        "delivery_ids": [],
        "rule": None,
        "confidence": None,
    }


def decide(trigger: dict, subscriber_ids: list[str], rules: dict) -> dict:
    """Decisão do fast path para um trigger `new` (sem escrever nada)."""
    route = empty_route()
    if subscriber_ids:
        demand_ids = sorted(subscriber_ids)
        route.update(
            by="subscription", demand_id=demand_ids[0], demand_ids=demand_ids
        )
        return {"state": "routed", "route": route, "rule": None}
    for rule in rules["rules"]:
        if not matches(rule, trigger):
            continue
        if rule["action"] == "ignore":
            route.update(by="rule", rule=rule["name"])
            return {"state": "ignored", "route": route, "rule": rule["name"]}
        route.update(by="rule", area=rule["area"], rule=rule["name"])
        return {"state": "routed", "route": route, "rule": rule["name"]}
    if rules["default"] == "ignore":
        return {"state": "ignored", "route": route, "rule": None}
    return {"state": "candidate", "route": route, "rule": None}


def transition(trigger: dict, to: str, now: str) -> dict:
    """Valida e aplica `trigger.state -> to`; devolve o trigger atualizado."""
    current = trigger.get("state", "")
    if to not in TRIGGER_TRANSITIONS:
        raise AtlasError(f"estado de trigger desconhecido: {to}")
    allowed = TRIGGER_TRANSITIONS.get(current, ())
    if to not in allowed:
        raise AtlasError(
            f"transição ilegal de trigger: {current} -> {to} "
            f"(permitidas: {', '.join(allowed) or 'nenhuma'})"
        )
    trigger["state"] = to
    trigger["updated_at"] = now
    return trigger
