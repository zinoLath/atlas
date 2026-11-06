"""Armazenamento: log transacional + JSONL canônico + SQLite materializado.

Toda escrita de estado passa por `Store.transaction()`: adquire `flock` em
`state/.lock`, carrega os snapshots, aplica as operações em memória, grava a
linha da transação com `fsync`, regrava os snapshots com `tmp+rename` e só
então grava `state/applied.json`. Um processo que morra entre a linha e os
snapshots é recuperado no próximo comando (replay das transações pendentes).
"""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from . import policy, render, triggers
from .notifications import migrate_fields
from .util import (
    AtlasError,
    fsync_dir,
    iso,
    now_dt,
    read_jsonl,
    write_jsonl,
    write_text,
)

DEFAULT_HOME = Path(__file__).resolve().parent.parent

SCHEMA_VERSION = 2
LOCK_TIMEOUT_SECONDS = 30.0
CRASH_ENV = "ATLAS_TEST_CRASH_AFTER"

SCHEMA = """
CREATE TABLE demands (
    id TEXT PRIMARY KEY,
    area TEXT NOT NULL,
    state TEXT NOT NULL,
    lane TEXT NOT NULL,
    title TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    due TEXT
);
CREATE TABLE kb_nodes (
    id TEXT PRIMARY KEY,
    parent TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    importance INTEGER NOT NULL,
    tags TEXT NOT NULL
);
CREATE TABLE kb_edges (
    src TEXT NOT NULL,
    rel TEXT NOT NULL,
    dst TEXT NOT NULL,
    weight REAL NOT NULL,
    note TEXT NOT NULL,
    PRIMARY KEY (src, dst, rel)
);
CREATE VIRTUAL TABLE kb_fts USING fts5(
    node_id UNINDEXED,
    title,
    body,
    tokenize = 'unicode61 remove_diacritics 2'
);
"""


def next_sequential_id(rows: list[dict], prefix: str, dt: datetime) -> str:
    """Próximo id `prefix-YYYYMMDD-NNNN` (max+1 no snapshot carregado)."""
    head = f"{prefix}-{dt.strftime('%Y%m%d')}-"
    highest = 0
    for row in rows:
        row_id = row.get("id", "")
        if not row_id.startswith(head):
            continue
        try:
            highest = max(highest, int(row_id.rsplit("-", 1)[1]))
        except (IndexError, ValueError):
            continue
    return f"{head}{highest + 1:04d}"


def _upsert(rows: list[dict], item: dict, key: str) -> None:
    for index, row in enumerate(rows):
        if row.get(key) == item.get(key):
            rows[index] = item
            return
    rows.append(item)


def _upsert_edge(rows: list[dict], edge: dict) -> None:
    ident = (edge.get("src"), edge.get("rel"), edge.get("dst"))
    for index, row in enumerate(rows):
        if (row.get("src"), row.get("rel"), row.get("dst")) == ident:
            rows[index] = edge
            return
    rows.append(edge)


def _upsert_subscription(rows: list[dict], item: dict) -> None:
    ident = (item.get("thread_id"), item.get("demand_id"))
    for index, row in enumerate(rows):
        if (row.get("thread_id"), row.get("demand_id")) == ident:
            rows[index] = item
            return
    rows.append(item)


def _upsert_delivery(rows: list[dict], item: dict) -> None:
    for index, row in enumerate(rows):
        if row.get("id") == item.get("id"):
            rows[index] = item
            return
    rows.append(item)


def apply_ops(ops: list[dict], view: "Snapshot") -> set[str]:
    """Aplica as operações de uma transação sobre os snapshots em memória."""
    touched: set[str] = set()
    for op in ops:
        name = op.get("op")
        if name == "demand.upsert":
            _upsert(view.demands, dict(op["demand"]), "id")
            touched.add("demands")
        elif name == "event.append":
            _upsert(view.events, dict(op["event"]), "seq")
            touched.add("events")
        elif name == "cursor.set":
            view.cursor[op["name"]] = op["value"]
            touched.add("cursor")
        elif name == "digest.append":
            _upsert(view.digests, dict(op["digest"]), "id")
            touched.add("digests")
        elif name in ("notification.put", "notification.update"):
            _upsert(view.notifications, dict(op["notification"]), "id")
            touched.add("notifications")
        elif name == "kb.upsert":
            _upsert(view.kb_nodes, dict(op["node"]), "id")
            touched.add("kb")
        elif name == "edge.upsert":
            _upsert_edge(view.edges, dict(op["edge"]))
            touched.add("edges")
        elif name == "trigger.put":
            _upsert(view.triggers, dict(op["trigger"]), "id")
            touched.add("triggers")
        elif name == "thread.put":
            _upsert(view.threads, dict(op["thread"]), "id")
            touched.add("threads")
        elif name == "subscription.put":
            _upsert_subscription(view.subscriptions, dict(op["subscription"]))
            touched.add("subscriptions")
        elif name == "delivery.put":
            _upsert_delivery(view.deliveries, dict(op["delivery"]))
            touched.add("deliveries")
        else:
            raise AtlasError(f"operação desconhecida na transação: {name!r}")
    if "events" in touched:
        view.events.sort(key=lambda event: int(event.get("seq", 0)))
    return touched


def _crash_point(name: str) -> None:
    """Hook de teste: morre como um kill -9 no meio do commit."""
    if os.environ.get(CRASH_ENV) == name:
        raise SystemExit(99)


class Snapshot:
    """Leitura consistente dos snapshots JSONL (sem lock)."""

    def __init__(self, store: "Store", *, empty: bool = False) -> None:
        self.store = store
        if empty:
            self.demands: list[dict] = []
            self.events: list[dict] = []
            self.kb_nodes: list[dict] = []
            self.edges: list[dict] = []
            self.digests: list[dict] = []
            self.notifications: list[dict] = []
            self.triggers: list[dict] = []
            self.threads: list[dict] = []
            self.subscriptions: list[dict] = []
            self.deliveries: list[dict] = []
            self.cursor: dict = {"last_digest_at": None}
        else:
            self.demands = store.demands()
            self.events = store.events()
            self.kb_nodes = store.kb_nodes()
            self.edges = store.edges()
            self.digests = store.digests()
            self.notifications = store.notifications()
            self.triggers = store.triggers()
            self.threads = store.threads()
            self.subscriptions = store.subscriptions()
            self.deliveries = store.deliveries()
            self.cursor = store.cursor()

    def next_demand_id(self) -> str:
        return next_sequential_id(self.demands, "dmd", self.store.dt)

    def next_digest_id(self) -> str:
        return next_sequential_id(self.digests, "dig", self.store.dt)

    def next_trigger_id(self) -> str:
        return next_sequential_id(self.triggers, "trg", self.store.dt)

    def next_thread_id(self) -> str:
        return next_sequential_id(self.threads, "thr", self.store.dt)

    def next_delivery_id(self) -> str:
        return next_sequential_id(self.deliveries, "dlv", self.store.dt)


class Transaction(Snapshot):
    """Unidade de commit: snapshots carregados sob `flock` + ops pendentes."""

    def __init__(self, store: "Store", cmd: str, actor: str = "cli") -> None:
        self.store = store
        self.cmd = cmd
        self.actor = actor
        self.tx_id = store.new_tx_id()
        self.ops: list[dict] = []
        self._touched: set[str] = set()
        self._after: list = []
        self._lock = None

    # -- ciclo de vida ---------------------------------------------------
    def __enter__(self) -> "Transaction":
        self._lock = self.store.lock()
        self._lock.__enter__()
        try:
            self.store._recover_locked()
            Snapshot.__init__(self, self.store)
            self.ops = []
            self._touched = set()
        except BaseException:
            self._lock.__exit__(*sys.exc_info())
            raise
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            if exc_type is None:
                self._commit()
                for callback in self._after:
                    callback()
        finally:
            self._lock.__exit__(exc_type, exc, tb)
        return False

    def on_commit(self, callback) -> None:
        """Roda `callback` depois do commit, ainda sob o lock (vistas derivadas)."""
        self._after.append(callback)

    # -- operações -------------------------------------------------------
    def upsert_demand(self, demand: dict) -> dict:
        item = dict(demand)
        _upsert(self.demands, item, "id")
        self.ops.append({"op": "demand.upsert", "demand": item})
        self._touched.add("demands")
        return item

    def append_event(self, event: dict) -> dict:
        item = dict(event)
        item["seq"] = max(
            (int(other.get("seq", 0)) for other in self.events), default=0
        ) + 1
        item["tx_id"] = self.tx_id
        return self.put_event(item)

    def put_event(self, event: dict) -> dict:
        """Grava um evento já numerado (replay/migração preservam o `seq`)."""
        item = dict(event)
        if "tx_id" not in item:
            item["tx_id"] = self.tx_id
        _upsert(self.events, item, "seq")
        self.events.sort(key=lambda other: int(other.get("seq", 0)))
        self.ops.append({"op": "event.append", "event": item})
        self._touched.add("events")
        return item

    def set_cursor(self, name: str, value) -> None:
        self.cursor[name] = value
        self.ops.append({"op": "cursor.set", "name": name, "value": value})
        self._touched.add("cursor")

    def append_digest(self, digest: dict) -> dict:
        item = dict(digest)
        _upsert(self.digests, item, "id")
        self.ops.append({"op": "digest.append", "digest": item})
        self._touched.add("digests")
        return item

    def put_notification(self, notification: dict) -> dict:
        """Grava a notificação no outbox (estado inicial antes do envio)."""
        item = dict(notification)
        _upsert(self.notifications, item, "id")
        self.ops.append({"op": "notification.put", "notification": item})
        self._touched.add("notifications")
        return item

    def update_notification(self, notification: dict) -> dict:
        item = dict(notification)
        _upsert(self.notifications, item, "id")
        self.ops.append({"op": "notification.update", "notification": item})
        self._touched.add("notifications")
        return item

    def upsert_kb(self, node: dict) -> dict:
        item = dict(node)
        _upsert(self.kb_nodes, item, "id")
        self.ops.append({"op": "kb.upsert", "node": item})
        self._touched.add("kb")
        return item

    def upsert_edge(self, edge: dict) -> dict:
        item = dict(edge)
        _upsert_edge(self.edges, item)
        self.ops.append({"op": "edge.upsert", "edge": item})
        self._touched.add("edges")
        return item

    def put_trigger(self, trigger: dict) -> dict:
        item = dict(trigger)
        _upsert(self.triggers, item, "id")
        self.ops.append({"op": "trigger.put", "trigger": item})
        self._touched.add("triggers")
        return item

    def put_thread(self, thread: dict) -> dict:
        item = dict(thread)
        _upsert(self.threads, item, "id")
        self.ops.append({"op": "thread.put", "thread": item})
        self._touched.add("threads")
        return item

    def put_subscription(self, subscription: dict) -> dict:
        item = dict(subscription)
        _upsert_subscription(self.subscriptions, item)
        self.ops.append({"op": "subscription.put", "subscription": item})
        self._touched.add("subscriptions")
        return item

    def put_delivery(self, delivery: dict) -> dict:
        item = dict(delivery)
        _upsert_delivery(self.deliveries, item)
        self.ops.append({"op": "delivery.put", "delivery": item})
        self._touched.add("deliveries")
        return item

    # -- commit ----------------------------------------------------------
    def _commit(self) -> None:
        self.store._write_schema_version()
        if self.ops:
            record = {
                "schema": SCHEMA_VERSION,
                "tx_id": self.tx_id,
                "cmd": self.cmd,
                "actor": self.actor,
                "at": self.store.now_iso,
                "ops": self.ops,
            }
            self.store._append_transaction(record)
            _crash_point("tx")
            self.store._write_snapshots(self, self._touched)
            _crash_point("snapshot")
            last_tx_id = self.tx_id
        else:
            last_tx_id = self.store._applied_last_tx_id()
        self.store._write_applied(last_tx_id)
        _crash_point("applied")


class Store:
    """Acesso ao repositório de estado. `home` pode vir de ATLAS_HOME."""

    def __init__(self, home: str | Path | None = None, now: str | None = None) -> None:
        base = home or os.environ.get("ATLAS_HOME") or DEFAULT_HOME
        self.home = Path(base).expanduser().resolve()
        self.dt = now_dt(now)
        self.now_iso = iso(self.dt)
        # `flock` não é reentrante no mesmo processo (um segundo open bloquearia
        # contra o primeiro): quem já está sob `lock()` usa `_lock_depth` para
        # chamar helpers `_*_unlocked` em vez de reabrir o arquivo.
        # `_lock_owner` registra a thread que segura o lock: outra thread na
        # mesma instância não pode pular a aquisição só porque o depth > 0.
        self._lock_depth = 0
        self._lock_owner: int | None = None

    # ---- caminhos -----------------------------------------------------
    @property
    def state_dir(self) -> Path:
        return self.home / "state"

    @property
    def events_path(self) -> Path:
        return self.state_dir / "events.jsonl"

    @property
    def demands_path(self) -> Path:
        return self.state_dir / "demands.jsonl"

    @property
    def kb_path(self) -> Path:
        return self.state_dir / "kb.jsonl"

    @property
    def edges_path(self) -> Path:
        return self.state_dir / "edges.jsonl"

    @property
    def digests_path(self) -> Path:
        return self.state_dir / "digests.jsonl"

    @property
    def notifications_path(self) -> Path:
        return self.state_dir / "notifications.jsonl"

    @property
    def triggers_path(self) -> Path:
        return self.state_dir / "triggers.jsonl"

    @property
    def threads_path(self) -> Path:
        return self.state_dir / "threads.jsonl"

    @property
    def subscriptions_path(self) -> Path:
        return self.state_dir / "subscriptions.jsonl"

    @property
    def deliveries_path(self) -> Path:
        return self.state_dir / "deliveries.jsonl"

    @property
    def rules_path(self) -> Path:
        return self.home / "router" / "rules.v1.json"

    @property
    def cursor_path(self) -> Path:
        return self.state_dir / "cursor.json"

    @property
    def transactions_path(self) -> Path:
        return self.state_dir / "transactions.jsonl"

    @property
    def applied_path(self) -> Path:
        return self.state_dir / "applied.json"

    @property
    def schema_path(self) -> Path:
        return self.state_dir / "schema_version"

    @property
    def lock_path(self) -> Path:
        return self.state_dir / ".lock"

    @property
    def db_path(self) -> Path:
        return self.state_dir / "hub.db"

    def demand_md(self, demand_id: str) -> Path:
        return self.home / "demands" / f"{demand_id}.md"

    def kb_md(self, node_id: str) -> Path:
        return self.home / "kb" / f"{node_id}.md"

    # ---- bootstrap ----------------------------------------------------
    def init(self) -> None:
        # `init` compete com `transaction` num home novo: dois processos podem
        # ver `transactions.jsonl` ausente e o segundo truncar o log do primeiro.
        # Todo o bootstrap roda sob o lock dos escritores (que cria `state/`).
        with self.lock():
            self._init_locked()

    def _init_locked(self) -> None:
        # Um layout criado do zero já nasce no schema 2; um home com estado
        # v1 preexistente só é marcado por `atlas migrate`.
        had_state = any(
            path.exists()
            for path in (
                self.events_path,
                self.demands_path,
                self.kb_path,
                self.edges_path,
                self.digests_path,
                self.notifications_path,
                self.triggers_path,
                self.threads_path,
                self.subscriptions_path,
                self.deliveries_path,
            )
        )
        for directory in (
            self.home / "state",
            self.home / "kb",
            self.home / "demands",
            self.home / "policy",
            self.home / "router",
        ):
            directory.mkdir(parents=True, exist_ok=True)
        for area in policy.AREAS:
            area_dir = self.home / "areas" / area
            area_dir.mkdir(parents=True, exist_ok=True)
            charter = area_dir / "CHARTER.md"
            if not charter.exists():
                write_text(
                    charter,
                    f"# {area}\n\nÁrea do ATLAS: {area}.\n",
                )
        for path in (
            self.events_path,
            self.demands_path,
            self.kb_path,
            self.edges_path,
            self.digests_path,
            self.notifications_path,
            self.triggers_path,
            self.threads_path,
            self.subscriptions_path,
            self.deliveries_path,
            self.transactions_path,
        ):
            if not path.exists():
                write_text(path, "")
        if not self.cursor_path.exists():
            write_text(self.cursor_path, json.dumps({"last_digest_at": None}, indent=2) + "\n")
        if not had_state and not self.applied_path.exists():
            self._write_schema_version()
            self._write_applied(self._applied_last_tx_id())
        policy_file = policy.policy_path(self.home)
        if not policy_file.exists():
            write_text(
                policy_file,
                json.dumps(policy.DEFAULT_POLICY, indent=2, ensure_ascii=False) + "\n",
            )
        if not self.rules_path.exists():
            write_text(
                self.rules_path,
                json.dumps(triggers.DEFAULT_RULES, indent=2, ensure_ascii=False) + "\n",
            )

    # ---- leitura/escrita canônica ------------------------------------
    def demands(self) -> list[dict]:
        return read_jsonl(self.demands_path)

    def kb_nodes(self) -> list[dict]:
        return read_jsonl(self.kb_path)

    def edges(self) -> list[dict]:
        return read_jsonl(self.edges_path)

    def events(self) -> list[dict]:
        return read_jsonl(self.events_path)

    def digests(self) -> list[dict]:
        return read_jsonl(self.digests_path)

    def notifications(self) -> list[dict]:
        return read_jsonl(self.notifications_path)

    def triggers(self) -> list[dict]:
        return read_jsonl(self.triggers_path)

    def threads(self) -> list[dict]:
        return read_jsonl(self.threads_path)

    def subscriptions(self) -> list[dict]:
        return read_jsonl(self.subscriptions_path)

    def deliveries(self) -> list[dict]:
        return read_jsonl(self.deliveries_path)

    def cursor(self) -> dict:
        if not self.cursor_path.exists():
            return {"last_digest_at": None}
        try:
            data = json.loads(self.cursor_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise AtlasError(f"cursor inválido: {exc.msg}") from exc
        return data if isinstance(data, dict) else {"last_digest_at": None}

    def snapshot(self) -> Snapshot:
        return Snapshot(self)

    # ---- transações ----------------------------------------------------
    def new_tx_id(self) -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        return f"tx-{stamp}-{uuid.uuid4().hex[:8]}"

    def transaction(self, cmd: str, actor: str = "cli") -> Transaction:
        return Transaction(self, cmd, actor)

    @contextmanager
    def lock(self) -> Iterator[None]:
        """`flock(LOCK_EX)` em `state/.lock` com timeout de 30 s."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
        acquired = False
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise AtlasError(
                            f"não foi possível obter {self.lock_path} em "
                            f"{LOCK_TIMEOUT_SECONDS:.0f}s (outro processo escreve no estado)"
                        )
                    time.sleep(0.01)
            acquired = True
            if self._lock_depth == 0:
                self._lock_owner = threading.get_ident()
            self._lock_depth += 1
            yield
        finally:
            if acquired:
                self._lock_depth -= 1
                if self._lock_depth == 0:
                    self._lock_owner = None
            os.close(fd)

    # -- log transacional ------------------------------------------------
    def _repair_transactions_tail(self) -> None:
        """Conserta a cauda de um append interrompido (sempre sob `flock`)."""
        path = self.transactions_path
        if not path.exists():
            return
        with path.open("r+b") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            if size == 0:
                return
            handle.seek(size - 1)
            if handle.read(1) == b"\n":
                return
            data = path.read_bytes()
            cut = data.rfind(b"\n")
            head = data[: cut + 1] if cut >= 0 else b""
            tail = data[cut + 1 :] if cut >= 0 else data
            complete = False
            try:
                complete = isinstance(json.loads(tail.decode("utf-8")), dict)
            except (UnicodeDecodeError, json.JSONDecodeError):
                complete = False
            if complete:
                # Registro íntegro, só faltou o terminador: preserva.
                handle.seek(0, os.SEEK_END)
                handle.write(b"\n")
            else:
                handle.truncate(len(head))
            handle.flush()
            os.fsync(handle.fileno())
        fsync_dir(path.parent)

    def _read_transactions(self) -> list[dict]:
        path = self.transactions_path
        if not path.exists():
            return []
        data = path.read_bytes()
        if data and not data.endswith(b"\n"):
            # Cauda sem terminador: só entra se o reparo já a validou.
            cut = data.rfind(b"\n")
            data = data[: cut + 1] if cut >= 0 else b""
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AtlasError(f"{path}: bytes UTF-8 inválidos") from exc
        rows: list[dict] = []
        for lineno, line in enumerate(text.splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise AtlasError(f"{path}:{lineno}: JSON inválido ({exc.msg})") from exc
            if not isinstance(obj, dict):
                raise AtlasError(f"{path}:{lineno}: esperado objeto JSON")
            rows.append(obj)
        return rows

    def _append_transaction(self, record: dict) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._repair_transactions_tail()
        line = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        with self.transactions_path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())

    def _applied_last_tx_id(self) -> str | None:
        path = self.applied_path
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise AtlasError(f"applied.json inválido: {exc.msg}") from exc
        if not isinstance(data, dict):
            return None
        value = data.get("last_tx_id")
        return value if isinstance(value, str) else None

    def _write_applied(self, tx_id: str | None) -> None:
        write_text(
            self.applied_path,
            json.dumps({"schema": SCHEMA_VERSION, "last_tx_id": tx_id}, indent=2) + "\n",
        )

    def schema_version(self) -> int | None:
        path = self.schema_path
        if not path.exists():
            return None
        try:
            return int(path.read_text(encoding="utf-8").strip())
        except ValueError:
            return None

    def _write_schema_version(self) -> None:
        if self.schema_version() != SCHEMA_VERSION:
            write_text(self.schema_path, f"{SCHEMA_VERSION}\n")

    def is_migrated(self) -> bool:
        return (
            self.schema_version() == SCHEMA_VERSION
            and self.applied_path.exists()
        )

    def _pending_transactions(self, txs: list[dict]) -> list[dict]:
        if not txs:
            return []
        applied = self._applied_last_tx_id()
        if applied is None:
            return txs
        ids = [tx.get("tx_id") for tx in txs]
        if applied not in ids:
            return txs
        return txs[ids.index(applied) + 1 :]

    def _recovery_needed(self) -> bool:
        txs = self._read_transactions()
        if not txs:
            return False
        return self._applied_last_tx_id() != txs[-1].get("tx_id")

    def recover(self) -> int:
        """Replaya transações pendentes; no caminho comum não pega o lock."""
        if not self._recovery_needed():
            return 0
        with self.lock():
            return self._recover_locked()

    def _recover_locked(self) -> int:
        self._repair_transactions_tail()
        txs = self._read_transactions()
        pending = self._pending_transactions(txs)
        if not pending:
            return 0
        view = Snapshot(self)
        touched: set[str] = set()
        for tx in pending:
            if int(tx.get("schema", 0)) != SCHEMA_VERSION:
                raise AtlasError(
                    f"transação {tx.get('tx_id')!r} com schema inválido; "
                    f"esperado {SCHEMA_VERSION}"
                )
            touched |= apply_ops(tx.get("ops") or [], view)
        self._write_snapshots(view, touched)
        self._write_schema_version()
        self._write_applied(txs[-1].get("tx_id"))
        return len(pending)

    def _write_snapshots(self, view: Snapshot, touched: set[str] | None = None) -> None:
        if touched is None or "demands" in touched:
            write_jsonl(self.demands_path, view.demands)
        if touched is None or "events" in touched:
            write_jsonl(self.events_path, view.events)
        if touched is None or "kb" in touched:
            write_jsonl(self.kb_path, view.kb_nodes)
        if touched is None or "edges" in touched:
            write_jsonl(self.edges_path, view.edges)
        if touched is None or "digests" in touched:
            write_jsonl(self.digests_path, view.digests)
        if touched is None or "notifications" in touched:
            write_jsonl(self.notifications_path, view.notifications)
        if touched is None or "triggers" in touched:
            write_jsonl(self.triggers_path, view.triggers)
        if touched is None or "threads" in touched:
            write_jsonl(self.threads_path, view.threads)
        if touched is None or "subscriptions" in touched:
            write_jsonl(self.subscriptions_path, view.subscriptions)
        if touched is None or "deliveries" in touched:
            write_jsonl(self.deliveries_path, view.deliveries)
        if touched is None or "cursor" in touched:
            write_text(
                self.cursor_path,
                json.dumps(view.cursor, indent=2) + "\n",
            )

    # ---- migração e rebuild -------------------------------------------
    def migrate(self) -> int:
        """Cria a transação de bootstrap do estado v1 (idempotente)."""
        with self.lock():
            self._recover_locked()
            if self.is_migrated():
                return 0
            tx = Transaction(self, "migrate.bootstrap")
            Snapshot.__init__(tx, self)
            for demand in list(tx.demands):
                demand.setdefault("lease_epoch", 0)
                for field in ("wake_at", "artifact", "waiting_on", "follow_up_at"):
                    demand.setdefault(field, None)
                migrate_fields(demand)
                tx.upsert_demand(demand)
            for event in list(tx.events):
                tx.put_event(event)
            if tx.cursor.get("last_digest_at") is not None:
                tx.set_cursor("last_digest_at", tx.cursor["last_digest_at"])
            for node in list(tx.kb_nodes):
                tx.upsert_kb(node)
            for edge in list(tx.edges):
                tx.upsert_edge(edge)
            for digest in list(tx.digests):
                tx.append_digest(digest)
            tx._commit()
            return len(tx.ops)

    def rebuild(self) -> None:
        """`rebuild` = replay de todas as transações a partir de zero."""
        with self.lock():
            self._repair_transactions_tail()
            txs = self._read_transactions()
            if txs:
                view = Snapshot(self, empty=True)
                for tx in txs:
                    if int(tx.get("schema", 0)) != SCHEMA_VERSION:
                        raise AtlasError(
                            f"transação {tx.get('tx_id')!r} com schema inválido; "
                            f"esperado {SCHEMA_VERSION}"
                        )
                    apply_ops(tx.get("ops") or [], view)
                self._write_snapshots(view)
                self._write_schema_version()
                self._write_applied(txs[-1].get("tx_id"))
            # As vistas são gravadas sob o mesmo lock: um `add` concorrente
            # não pode intercalar corpo/commit entre o replay e o render.
            self.rebuild_views()

    # ---- SQLite -------------------------------------------------------
    def connect(self, path: Path | None = None) -> sqlite3.Connection:
        conn = sqlite3.connect(path or self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def rebuild_db(self) -> None:
        """Reconstrói `hub.db` sob o lock dos escritores.

        `Transaction._commit`/`rebuild_views` já seguram o lock quando chamam
        isto; `kb.index`/`sync_db` não. Só pula a aquisição quando a própria
        thread segura o lock (o depth sozinho não prova propriedade: outra
        thread na mesma instância veria `depth > 0` e publicaria sem `flock`).
        """
        if self._lock_depth > 0 and self._lock_owner == threading.get_ident():
            self._rebuild_db_unlocked()
            return
        with self.lock():
            self._rebuild_db_unlocked()

    def _rebuild_db_unlocked(self) -> None:
        # Constrói em arquivo temporário exclusivo e troca atomicamente: um
        # crash no meio do rebuild nunca deixa um hub.db parcial, e dois
        # escritores nunca disputam um `hub.db.tmp` fixo.
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=self.db_path.name + ".", suffix=".tmp", dir=self.db_path.parent
        )
        os.close(fd)
        tmp = Path(tmp_name)
        demand_rows = self.demands()
        nodes = self.kb_nodes()
        edges = self.edges()
        conn = self.connect(tmp)
        try:
            conn.executescript(SCHEMA)
            conn.executemany(
                "INSERT INTO demands(id, area, state, lane, title, updated_at, due) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        d.get("id", ""),
                        d.get("area", ""),
                        d.get("state", ""),
                        d.get("lane") or "",
                        d.get("title", ""),
                        d.get("updated_at", ""),
                        d.get("due"),
                    )
                    for d in demand_rows
                ],
            )
            conn.executemany(
                "INSERT INTO kb_nodes(id, parent, kind, title, summary, importance, tags) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        n.get("id", ""),
                        n.get("parent", ""),
                        n.get("kind", ""),
                        n.get("title", ""),
                        n.get("summary", ""),
                        int(n.get("importance", 3)),
                        ",".join(n.get("tags") or []),
                    )
                    for n in nodes
                ],
            )
            conn.executemany(
                "INSERT INTO kb_edges(src, rel, dst, weight, note) VALUES (?, ?, ?, ?, ?)",
                [
                    (
                        e.get("src", ""),
                        e.get("rel", ""),
                        e.get("dst", ""),
                        float(e.get("weight", 0.5)),
                        e.get("note", ""),
                    )
                    for e in edges
                ],
            )
            conn.executemany(
                "INSERT INTO kb_fts(node_id, title, body) VALUES (?, ?, ?)",
                [
                    (
                        n.get("id", ""),
                        n.get("title", ""),
                        render.read_body(self.kb_md(n.get("id", ""))),
                    )
                    for n in nodes
                ],
            )
            conn.commit()
        except BaseException:
            conn.close()
            for suffix in ("", "-journal", "-wal", "-shm"):
                Path(str(tmp) + suffix).unlink(missing_ok=True)
            raise
        else:
            conn.close()
        os.replace(tmp, self.db_path)

    def sync_db(self) -> None:
        self.rebuild_db()

    def ensure_db(self) -> None:
        if not self.db_path.exists():
            self.rebuild_db()
            return
        try:
            newest_source = max(
                self.kb_path.stat().st_mtime,
                self.edges_path.stat().st_mtime,
            )
            if newest_source > self.db_path.stat().st_mtime:
                self.rebuild_db()
        except FileNotFoundError:
            self.rebuild_db()

    # ---- render -------------------------------------------------------
    def render_demand(self, demand: dict) -> None:
        render.render_demand(self.demand_md(demand["id"]), demand)

    def render_kb(self, node: dict) -> None:
        render.render_kb(self.kb_md(node["id"]), node)

    def rebuild_views(self) -> None:
        for demand in self.demands():
            self.render_demand(demand)
        for node in self.kb_nodes():
            self.render_kb(node)
        self.rebuild_db()
