"""Testes do M1a.1: transação, lock, recuperação e estados de espera."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
BIN = REPO / "bin" / "atlas"
TMP_ROOT = REPO / "tests" / ".tmp"
NOW = "2026-11-01T14:00:00-03:00"
LEASE = "2026-11-01T15:00:00-03:00"

sys.path.insert(0, str(REPO))

from atlas_cli.store import Store  # noqa: E402
from atlas_cli.util import AtlasError  # noqa: E402


class M1a1TestCase(unittest.TestCase):
    def setUp(self) -> None:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.home = self.make_home()

    def tearDown(self) -> None:
        shutil.rmtree(self.home, ignore_errors=True)

    # ---- helpers ------------------------------------------------------
    def make_home(self) -> Path:
        home = Path(tempfile.mkdtemp(prefix="atlas-m1a1-", dir=TMP_ROOT))
        env = dict(os.environ, ATLAS_HOME=str(home))
        proc = subprocess.run(
            [sys.executable, str(BIN), "--now", NOW, "init"],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return home

    def run_atlas(
        self,
        home: Path,
        *args: str,
        now: str = NOW,
        env_extra: dict | None = None,
    ) -> subprocess.CompletedProcess:
        env = dict(os.environ, ATLAS_HOME=str(home))
        env.update(env_extra or {})
        return subprocess.run(
            [sys.executable, str(BIN), "--now", now, *args],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )

    def ok(self, *args: str, now: str = NOW, home: Path | None = None) -> str:
        proc = self.run_atlas(home or self.home, *args, now=now)
        self.assertEqual(proc.returncode, 0, f"stdout={proc.stdout}\nstderr={proc.stderr}")
        return proc.stdout

    def fail(self, *args: str, code: int = 2, now: str = NOW, home: Path | None = None):
        proc = self.run_atlas(home or self.home, *args, now=now)
        self.assertEqual(
            proc.returncode, code, f"stdout={proc.stdout}\nstderr={proc.stderr}"
        )
        return proc

    def jsonl(self, name: str, home: Path | None = None) -> list[dict]:
        path = (home or self.home) / "state" / name
        text = path.read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    def write_jsonl(self, name: str, rows: list[dict], home: Path | None = None) -> None:
        path = (home or self.home) / "state" / name
        path.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
            encoding="utf-8",
        )

    def add(self, title: str = "Demanda teste", home: Path | None = None) -> str:
        out = self.ok(
            "add", "--area", "projetos", "--title", title, home=home
        )
        return out.split()[0]

    def advance(self, demand_id: str, *states: str) -> None:
        for state in states:
            self.ok("transition", demand_id, state)

    def demand(self, demand_id: str, home: Path | None = None) -> dict:
        rows = self.jsonl("demands.jsonl", home)
        return {row["id"]: row for row in rows}[demand_id]

    def to_ready(self, home: Path | None = None, title: str = "Demanda teste") -> str:
        demand_id = self.add(title, home)
        self.advance(demand_id, "routed", "triaged", "planned", "ready")
        return demand_id

    # ---- 1. concorrência ---------------------------------------------
    def test_concurrent_adds_commit_eight_distinct_demands(self) -> None:
        env = dict(os.environ, ATLAS_HOME=str(self.home))
        procs = [
            subprocess.Popen(
                [
                    sys.executable, str(BIN), "--now", NOW,
                    "add", "--area", "projetos", "--title", f"Concorrente {index}",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            for index in range(8)
        ]
        for proc in procs:
            out, err = proc.communicate(timeout=180)
            self.assertEqual(proc.returncode, 0, f"{out}\n{err}")

        demands = self.jsonl("demands.jsonl")
        events = self.jsonl("events.jsonl")
        transactions = self.jsonl("transactions.jsonl")
        self.assertEqual(len(demands), 8)
        self.assertEqual(len({row["id"] for row in demands}), 8)
        self.assertEqual(len(events), 8)
        self.assertEqual(sorted(row["seq"] for row in events), list(range(1, 9)))
        self.assertEqual(len(transactions), 8)
        self.assertEqual(len({tx["tx_id"] for tx in transactions}), 8)

        self.ok("rebuild")
        conn = sqlite3.connect(self.home / "state" / "hub.db")
        try:
            total, distinct = conn.execute(
                "SELECT COUNT(*), COUNT(DISTINCT id) FROM demands"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual((total, distinct), (8, 8))

    def test_init_usa_o_lock_dos_escritores(self) -> None:
        home = Path(tempfile.mkdtemp(prefix="atlas-m1a1-lock-", dir=TMP_ROOT))
        self.addCleanup(shutil.rmtree, home, True)
        store = Store(home=home, now=NOW)
        store.state_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(store.lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        with mock.patch("atlas_cli.store.LOCK_TIMEOUT_SECONDS", 0.2):
            with self.assertRaises(AtlasError) as ctx:
                store.init()
        self.assertIn(str(store.lock_path), str(ctx.exception))
        # Nada foi escrito: o bootstrap inteiro ficou atrás do lock.
        self.assertFalse(store.demands_path.exists())
        self.assertFalse(store.transactions_path.exists())

        fcntl.flock(fd, fcntl.LOCK_UN)
        store.init()
        self.assertTrue(store.demands_path.exists())
        self.assertTrue(store.transactions_path.exists())

    def test_inits_concorrentes_nao_truncam_transacao(self) -> None:
        home = Path(tempfile.mkdtemp(prefix="atlas-m1a1-init-", dir=TMP_ROOT))
        self.addCleanup(shutil.rmtree, home, True)
        env = dict(os.environ, ATLAS_HOME=str(home))
        inits = [
            subprocess.Popen(
                [sys.executable, str(BIN), "--now", NOW, "init"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            for _ in range(2)
        ]
        add = subprocess.Popen(
            [
                sys.executable, str(BIN), "--now", NOW,
                "add", "--area", "projetos", "--title", "Depois do init",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        for proc in [*inits, add]:
            out, err = proc.communicate(timeout=180)
            self.assertEqual(proc.returncode, 0, f"{out}\n{err}")

        self.assertEqual(len(self.jsonl("demands.jsonl", home)), 1)
        self.assertEqual(len(self.jsonl("transactions.jsonl", home)), 1)
        self.ok("rebuild", home=home)
        self.assertEqual(len(self.jsonl("demands.jsonl", home)), 1)

    # ---- 2. crash em cada fronteira ----------------------------------
    def test_crash_boundaries_recover_exactly_once(self) -> None:
        for point in ("tx", "snapshot", "applied"):
            with self.subTest(point=point):
                home = self.make_home()
                proc = self.run_atlas(
                    home,
                    "add", "--area", "projetos", "--title", f"Crash {point}",
                    env_extra={"ATLAS_TEST_CRASH_AFTER": point},
                )
                self.assertEqual(proc.returncode, 99, proc.stderr)

                # Qualquer comando abre o store e recupera antes de servir.
                self.assertEqual(self.ok("doctor", home=home), "doctor ok\n")
                self.assertEqual(len(self.jsonl("demands.jsonl", home)), 1)
                self.assertEqual(len(self.jsonl("events.jsonl", home)), 1)
                transactions = self.jsonl("transactions.jsonl", home)
                self.assertEqual(len(transactions), 1)
                applied = json.loads(
                    (home / "state" / "applied.json").read_text(encoding="utf-8")
                )
                self.assertEqual(applied["last_tx_id"], transactions[0]["tx_id"])

                self.ok("rebuild", home=home)
                self.assertEqual(len(self.jsonl("demands.jsonl", home)), 1)
                self.assertEqual(len(self.jsonl("events.jsonl", home)), 1)
                shutil.rmtree(home, ignore_errors=True)

    # ---- 3. fencing, epoch e estados de espera -----------------------
    def test_expire_bumps_epoch_and_stale_epoch_is_rejected(self) -> None:
        demand_id = self.to_ready()
        self.ok("transition", demand_id, "running", "--owner", "zino", "--lease", LEASE)
        self.assertEqual(self.demand(demand_id)["lease_epoch"], 1)

        self.ok("expire", now="2026-11-01T16:00:00-03:00")
        demand = self.demand(demand_id)
        self.assertEqual(demand["state"], "ready")
        self.assertEqual(demand["lease_epoch"], 2)
        self.assertEqual(
            self.jsonl("events.jsonl")[-1]["reason"], "lease_expired"
        )

        self.ok(
            "transition", demand_id, "running",
            "--owner", "zino",
            "--lease", "2026-11-01T17:00:00-03:00",
            "--expected-epoch", "2",
        )
        self.assertEqual(self.demand(demand_id)["lease_epoch"], 3)

        proc = self.fail(
            "transition", demand_id, "done", "--result", "ok", "--expected-epoch", "1"
        )
        self.assertIn("lease_epoch", proc.stderr)
        self.assertEqual(self.demand(demand_id)["state"], "running")

        self.ok(
            "transition", demand_id, "done", "--result", "ok", "--expected-epoch", "3"
        )
        self.assertEqual(self.demand(demand_id)["state"], "done")

    def test_waiting_review_and_external_field_contracts(self) -> None:
        review_id = self.to_ready(title="Revisar PR")
        self.ok(
            "transition", review_id, "running", "--owner", "zino", "--lease", LEASE
        )
        self.fail("transition", review_id, "waiting_review")
        self.ok("transition", review_id, "waiting_review", "--artifact", "pr-42")
        review = self.demand(review_id)
        self.assertEqual(review["state"], "waiting_review")
        self.assertEqual(review["artifact"], "pr-42")
        self.assertIsNone(review["owner"])

        self.fail("transition", review_id, "ready")
        self.ok("transition", review_id, "ready", "--reason", "review_rejected")
        review = self.demand(review_id)
        self.assertEqual(review["state"], "ready")
        self.assertIsNone(review["artifact"])

        external_id = self.to_ready(title="Aguardar Ana")
        self.ok(
            "transition", external_id, "running", "--owner", "zino", "--lease", LEASE
        )
        self.fail("transition", external_id, "waiting_external", "--waiting-on", "ana")
        self.fail(
            "transition", external_id, "waiting_external",
            "--follow-up-at", "2026-11-02T10:00:00-03:00",
        )
        self.ok(
            "transition", external_id, "waiting_external",
            "--waiting-on", "ana",
            "--follow-up-at", "2026-11-02T10:00:00-03:00",
        )
        external = self.demand(external_id)
        self.assertEqual(external["waiting_on"], "ana")
        self.assertIsNone(external["owner"])
        self.ok("transition", external_id, "ready", "--reason", "follow_up")

    def test_done_from_waiting_user_and_triaged_needs_no_lease(self) -> None:
        waiting = self.add("Aguardando usuário")
        self.advance(waiting, "routed", "triaged", "waiting_user")
        self.ok("transition", waiting, "done", "--result", "manual")
        self.assertEqual(self.demand(waiting)["state"], "done")

        triaged = self.add("Descartar na triagem")
        self.advance(triaged, "routed", "triaged")
        self.ok("transition", triaged, "done", "--result", "manual")
        self.assertEqual(self.demand(triaged)["state"], "done")
        self.assertEqual(self.jsonl("events.jsonl")[-1]["actor"], "cli")

    def test_snoozed_wakes_on_expire(self) -> None:
        demand_id = self.to_ready()
        self.fail("transition", demand_id, "snoozed")
        self.ok(
            "transition", demand_id, "snoozed",
            "--wake-at", "2026-11-02T09:00:00-03:00",
        )
        self.assertEqual(self.demand(demand_id)["state"], "snoozed")

        self.ok("expire", now="2026-11-02T08:00:00-03:00")
        self.assertEqual(self.demand(demand_id)["state"], "snoozed")

        self.ok("expire", now="2026-11-02T10:00:00-03:00")
        demand = self.demand(demand_id)
        self.assertEqual(demand["state"], "ready")
        self.assertIsNone(demand["wake_at"])
        last = self.jsonl("events.jsonl")[-1]
        self.assertEqual((last["from"], last["to"], last["actor"]), ("snoozed", "ready", "system"))

    # ---- 4. migração --------------------------------------------------
    def test_migrate_bootstraps_v1_state_idempotently(self) -> None:
        # Home v1 "cru": estado no disco e nenhum marcador de schema 2.
        home = Path(tempfile.mkdtemp(prefix="atlas-v1-", dir=TMP_ROOT))
        (home / "state").mkdir()
        self.addCleanup(shutil.rmtree, home, True)
        v1_demand = {
            "id": "dmd-20261031-0001",
            "area": "projetos",
            "title": "Demanda v1",
            "state": "ready",
            "created_at": "2026-10-31T10:00:00-03:00",
            "updated_at": "2026-10-31T10:05:00-03:00",
            "lane": "auto_flash",
            "rollover": {"digest_count": 0, "first_seen": "2026-10-31T10:00:00-03:00"},
        }
        v1_event = {
            "seq": 1,
            "ts": "2026-10-31T10:05:00-03:00",
            "demand_id": v1_demand["id"],
            "from": "new",
            "to": "ready",
            "actor": "cli",
            "reason": "",
            "run_id": "",
        }
        self.write_jsonl("demands.jsonl", [v1_demand], home)
        self.write_jsonl("events.jsonl", [v1_event], home)
        self.assertFalse((home / "state" / "schema_version").exists())

        out = self.ok("migrate", home=home)
        self.assertIn("schema 2 aplicado", out)
        self.assertEqual((home / "state" / "schema_version").read_text().strip(), "2")
        transactions = self.jsonl("transactions.jsonl", home)
        self.assertEqual(len(transactions), 1)
        self.assertEqual(transactions[0]["cmd"], "migrate.bootstrap")
        self.assertEqual(len(transactions[0]["ops"]), 2)
        migrated = self.jsonl("demands.jsonl", home)
        self.assertEqual(len(migrated), 1)
        self.assertEqual(migrated[0]["id"], v1_demand["id"])
        self.assertEqual(migrated[0]["lease_epoch"], 0)
        self.assertIsNone(migrated[0]["wake_at"])
        self.assertEqual(len(self.jsonl("events.jsonl", home)), 1)

        self.assertIn("já aplicado", self.ok("migrate", home=home))
        self.assertEqual(len(self.jsonl("transactions.jsonl", home)), 1)

        self.ok("rebuild", home=home)
        self.assertEqual(self.jsonl("demands.jsonl", home), migrated)
        self.assertEqual(len(self.jsonl("events.jsonl", home)), 1)
        self.assertEqual(self.ok("doctor", home=home), "doctor ok\n")

    # ---- 5. rebuild: log é autoritativo -------------------------------
    def test_rebuild_replays_transactions_from_zero(self) -> None:
        first = self.add("Primeira")
        self.add("Segunda")
        self.advance(first, "routed", "triaged")
        before = self.jsonl("demands.jsonl")
        self.assertEqual(len(self.jsonl("events.jsonl")), 4)

        self.write_jsonl("demands.jsonl", [])
        self.write_jsonl("events.jsonl", [])
        self.ok("rebuild")

        self.assertEqual(self.jsonl("demands.jsonl"), before)
        self.assertEqual(len(self.jsonl("events.jsonl")), 4)

    # ---- 6. events mostra tx_id --------------------------------------
    def test_events_report_transaction_id(self) -> None:
        demand_id = self.add()
        self.advance(demand_id, "routed")
        out = self.ok("events")
        tx_ids = {tx["tx_id"] for tx in self.jsonl("transactions.jsonl")}
        lines = out.splitlines()
        self.assertEqual(len(lines), 2)
        for line in lines:
            self.assertTrue(any(tx_id in line for tx_id in tx_ids), line)

    # ---- 7. doctor ----------------------------------------------------
    def test_doctor_flags_missing_schema_and_running_without_lease(self) -> None:
        self.assertEqual(self.ok("doctor"), "doctor ok\n")

        (self.home / "state" / "schema_version").unlink()
        proc = self.fail("doctor", code=1)
        self.assertIn("migrate", proc.stdout)

        self.ok("migrate")
        demand_id = self.add()
        rows = self.jsonl("demands.jsonl")
        rows[0]["state"] = "running"
        self.write_jsonl("demands.jsonl", rows)
        proc = self.fail("doctor", code=1)
        self.assertIn("running sem lease", proc.stdout)
        self.assertIn(demand_id, proc.stdout)


if __name__ == "__main__":
    unittest.main()
