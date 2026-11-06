"""Ingestão de triggers: biblioteca compartilhada pelo CLI e pelo reader.

`add_trigger` é a única porta de escrita de trigger. Valida o spec, grava o
blob content-addressed antes da transação, é idempotente por
`(source, external_id)` e devolve o trigger resultante. Não imprime nada e
não depende de argparse — o reader passa um `dict` do parser M1b.1a.
"""

from __future__ import annotations

import copy
import os

from . import triggers
from .store import Store
from .util import AtlasError, iso, parse_dt, sha256_text, write_text

CRASH_AFTER_BLOB_ENV = "ATLAS_TEST_CRASH_AFTER_TRIGGER_BLOB"

#: Campos do próprio trigger que `meta` nunca pode carregar: `meta` é
#: metadado do reader, não um segundo canal para identidade/ponteiros.
IDENTITY_KEYS = frozenset(
    {
        "id",
        "source",
        "external_id",
        "thread_id",
        "body_path",
        "body_sha256",
        "state",
        "route",
        "received_at",
        "title",
        "actor",
        "kind",
        "untrusted",
        "created_at",
        "updated_at",
    }
)

#: Marca transitória no retorno de `add_trigger` (nunca persistida).
REPAIRED_KEY = "repaired"


def _required_text(spec: dict, key: str, message: str) -> str:
    value = spec.get(key)
    if not isinstance(value, str):
        raise AtlasError(message)
    value = value.strip()
    if not value:
        raise AtlasError(message)
    return value


def _build_meta(spec: dict, thread_key: str) -> dict:
    raw = spec.get("meta")
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise AtlasError("meta deve ser um objeto")
    meta = copy.deepcopy(raw)

    forbidden = sorted(IDENTITY_KEYS.intersection(meta))
    if forbidden:
        raise AtlasError(
            "meta não pode sobrescrever campo do trigger: " + ", ".join(forbidden)
        )
    if "thread_key" in meta and meta["thread_key"] != thread_key:
        raise AtlasError(
            f"meta.thread_key diverge do thread_key do trigger: {meta['thread_key']!r}"
        )

    labels = spec.get("labels")
    if labels is None:
        labels = meta.get("labels") or []
    if not isinstance(labels, list) or not all(
        isinstance(label, str) for label in labels
    ):
        raise AtlasError("labels devem ser uma lista de strings")
    meta["labels"] = list(labels)
    return meta


def _write_blob(store: Store, body: str, body_sha: str, body_rel: str) -> bool:
    """Grava (ou confere) o blob content-addressed; devolve se já existia."""
    body_abs = store.home / body_rel
    if body_abs.exists():
        try:
            stored_text = body_abs.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise AtlasError(f"blob ilegível: {body_abs}: {exc}") from exc
        if sha256_text(stored_text) != body_sha:
            raise AtlasError(f"blob com hash divergente: {body_abs}")
        return True
    write_text(body_abs, body)
    return False


def _settle_duplicate(
    store: Store,
    tx,
    duplicate: dict,
    body_sha: str,
    body_rel: str,
    blob_preexisting: bool,
) -> dict:
    """Idempotência `(source, external_id)`: devolve, repara ou falha."""
    item = dict(duplicate)
    path = item.get("body_path")
    expected = item.get("body_sha256")
    blob = store.home / path if path else None
    restored_now = path == body_rel and not blob_preexisting

    if path and blob.exists() and expected is not None and not restored_now:
        try:
            stored_text = blob.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise AtlasError(f"corpo ilegível: {blob}: {exc}") from exc
        if sha256_text(stored_text) != expected:
            raise AtlasError(f"corpo adulterado: {item['id']} ({path})")
        if body_sha != expected:
            raise AtlasError(
                f"mesmo (source, external_id) com corpo diferente: {item['id']}"
            )
        return item

    if expected is not None and body_sha != expected:
        raise AtlasError(
            f"mesmo (source, external_id) com corpo diferente: {item['id']}"
        )
    # Corpo sumiu (ou metadado antigo): repara o ponteiro para o blob atual.
    item["body_path"] = body_rel
    item["body_sha256"] = body_sha
    item["updated_at"] = store.now_iso
    tx.put_trigger(item)
    return dict(item, **{REPAIRED_KEY: True})


def add_trigger(store: Store, spec: dict, *, actor: str = "reader") -> dict:
    """Grava (idempotentemente) um trigger `new` a partir de `spec`.

    Devolve o trigger novo, existente ou reparado; o reparo sinaliza com
    `trigger["repaired"] is True`. `actor` é o autor da transação.
    """
    if not isinstance(spec, dict):
        raise AtlasError("spec deve ser um objeto")

    source = _required_text(spec, "source", "--source vazio")
    external_id = _required_text(spec, "external_id", "--external-id vazio")
    title = _required_text(spec, "title", "título vazio")

    body = spec.get("body", "")
    if not isinstance(body, str):
        raise AtlasError("corpo deve ser string")

    raw_thread_key = spec.get("thread_key")
    if raw_thread_key is None:
        thread_key = f"{source}:{external_id}"
    elif isinstance(raw_thread_key, str) and raw_thread_key.strip():
        thread_key = raw_thread_key.strip()
    else:
        raise AtlasError("thread_key vazio")

    trigger_actor = spec.get("actor")
    if trigger_actor is None:
        trigger_actor = ""
    if not isinstance(trigger_actor, str):
        raise AtlasError("actor deve ser string")

    raw_received = spec.get("received_at")
    received_at = iso(parse_dt(raw_received)) if raw_received else store.now_iso

    kind = spec.get("kind") or source
    if not isinstance(kind, str):
        raise AtlasError("kind deve ser string")

    untrusted = spec.get("untrusted", True)
    if not isinstance(untrusted, bool):
        raise AtlasError("untrusted deve ser booleano")
    if spec.get("trusted"):
        if source != "manual":
            raise AtlasError("--trusted só é aceito com --source manual")
        untrusted = False

    meta = _build_meta(spec, thread_key)

    # BLOB-01: o corpo é content-addressed e durável antes do commit do trigger.
    body_sha = sha256_text(body)
    body_rel = triggers.blob_relpath(body_sha)
    blob_preexisting = _write_blob(store, body, body_sha, body_rel)
    if CRASH_AFTER_BLOB_ENV in os.environ:
        raise SystemExit(98)

    with store.transaction("trigger.add", actor=actor) as tx:
        duplicate = next(
            (
                row
                for row in tx.triggers
                if row.get("source") == source
                and row.get("external_id") == external_id
            ),
            None,
        )
        if duplicate is not None:
            return _settle_duplicate(
                store, tx, duplicate, body_sha, body_rel, blob_preexisting
            )

        thread = next(
            (
                row
                for row in tx.threads
                if row.get("source") == source and row.get("external_id") == thread_key
            ),
            None,
        )
        now = store.now_iso
        if thread is not None:
            thread = dict(thread)
            last = thread.get("last_trigger_at")
            if not last or parse_dt(received_at) > parse_dt(last):
                thread["last_trigger_at"] = received_at
            thread["updated_at"] = now
        else:
            thread = {
                "id": tx.next_thread_id(),
                "source": source,
                "external_id": thread_key,
                "title": title,
                "state": "open",
                "last_trigger_at": received_at,
                "created_at": now,
                "updated_at": now,
            }
        tx.put_thread(thread)

        trigger = {
            "id": tx.next_trigger_id(),
            "source": source,
            "external_id": external_id,
            "thread_id": thread["id"],
            "thread_key": thread_key,
            "received_at": received_at,
            "actor": trigger_actor,
            "kind": kind,
            "title": title,
            "body_path": body_rel,
            "body_sha256": body_sha,
            "untrusted": untrusted,
            "state": "new",
            "route": triggers.empty_route(),
            "meta": meta,
            "created_at": now,
            "updated_at": now,
        }
        tx.put_trigger(trigger)
        return trigger
