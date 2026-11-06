"""Política v1: lanes, orçamento e ações proibidas para automação."""

from __future__ import annotations

import json
from pathlib import Path

from .util import AtlasError

AREAS = (
    "projetos",
    "faculdade",
    "financas",
    "tempo",
    "internet",
    "mensagens",
)

LANE_ORDER = (
    "observe",
    "auto_flash",
    "flash_review",
    "approve_premium",
    "human",
)

DEFAULT_POLICY: dict = {
    "version": 1,
    "digest": {"threshold": 7, "rollover_max_hours": 12, "briefing_hour": 8},
    "budget": {"premium_daily_usd": 2.0, "max_auto_actions_per_hour": 6},
    "forbidden_auto": [
        "pay",
        "transfer",
        "purchase",
        "send_message",
        "publish",
        "delete",
        "merge_main",
        "credential",
        "install",
        "system_change",
    ],
    "lane_by_agent_level": {
        "0": "observe",
        "1": "auto_flash",
        "2": "flash_review",
        "3": "approve_premium",
        "4": "human",
    },
}

DEFAULT_CLASSIFICATION: dict = {
    "difficulty": 1,
    "complexity": 1,
    "importance": 3,
    "urgency": 1,
    "responsibility": "agent",
    "agent_level": 1,
    "confidence": 0.7,
    "rationale": "",
}


def policy_path(home: Path) -> Path:
    return home / "policy" / "v1.json"


def _deep_merge(base: dict, override: dict) -> dict:
    """Mescla recursivamente: valores do override vencem, base preenche o resto."""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_policy(home: Path) -> dict:
    path = policy_path(home)
    defaults = json.loads(json.dumps(DEFAULT_POLICY))
    if not path.exists():
        return defaults
    try:
        with path.open("r", encoding="utf-8") as fh:
            policy = json.load(fh)
    except json.JSONDecodeError as exc:
        raise AtlasError(f"policy inválida em {path}: {exc.msg}") from exc
    if not isinstance(policy, dict):
        raise AtlasError(f"policy inválida em {path}: esperado objeto JSON")
    return _deep_merge(defaults, policy)


def _lane_index(lane: str) -> int:
    try:
        return LANE_ORDER.index(lane)
    except ValueError:
        return LANE_ORDER.index("human")


def _raise_to(current: str, target: str) -> str:
    return target if _lane_index(target) > _lane_index(current) else current


def compute_lane(classification: dict, policy: dict | None = None) -> str:
    """Calcula a lane; regras apenas sobem, nunca descem."""
    policy = policy or DEFAULT_POLICY
    level = str(classification.get("agent_level", 0))
    mapping = policy.get("lane_by_agent_level", DEFAULT_POLICY["lane_by_agent_level"])
    lane = mapping.get(level, "human")

    confidence = float(classification.get("confidence", 1.0))
    if classification.get("responsibility") == "me" or confidence < 0.6:
        lane = _raise_to(lane, "human")

    if (
        int(classification.get("importance", 0)) == 5
        and int(classification.get("difficulty", 0)) >= 4
    ):
        lane = _raise_to(lane, "approve_premium")

    return lane
