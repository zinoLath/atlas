"""Testes do M1b.0e: deliveries por subscriber (claim/ack/lease/epoch)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BIN = REPO / "bin" / "atlas"
TMP_ROOT = REPO / "tests" / ".tmp"
NOW = "2026-11-01T14:00:00-03:00"


class M1b0eTestCase(unittest.TestCase):
    def setUp(self) -> None:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.home = Path(tempfile.mkdtemp(prefix="atlas-m1b0e-", dir=TMP_ROOT))
        self.env = dict(os.environ, ATLAS_HOME=str(self.home))
        self.ok("init")

    def tearDown(self) -> None:
        shutil.rmtree(self.home, ignore_errors=True)

    # ---- helpers ------------------------------------------------------
    def run_atlas(self, *args: str, now: str = NOW) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(BIN), "--now", now, *args],
            capture_output=True,
            text=True,
            env=self.env,
            check=False,
        )

    def ok(self, *args: str, now: str = NOW) -> str:
        proc = self.run_atlas(*args, now=now)
        self.assertEqual(proc.returncode, 0, f"stdout={proc.stdout}\nstderr={proc.stderr}")
        return proc.stdout

    def run_fail(self, *args: str, code: int = 2, now: str = NOW) -> subprocess.CompletedProcess:
        proc = self.run_atlas(*args, now=now)
        self.assertEqual(
            proc.returncode, code, f"stdout={proc.stdout}\nstderr={proc.stderr}"
        )
        return proc

    def jsonl(self, name: str) -> list[dict]:
        path = self.home / "state" / name
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def write_jsonl(self, name: str, rows: list[dict]) -> None:
        path = self.home / "state" / name
        path.write_text(
            "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
            encoding="utf-8",
        )

    def add_demand(self, area: str = "projetos", title: str = "Demanda teste") -> str:
        return self.ok("add", "--area", area, "--title", title).split()[0]

    def add_trigger(
        self, external_id: str, *extra: str, title: str = "Evento teste"
    ) -> tuple[str, str]:
        out = self.ok(
            "trigger",
            "add",
            "--source",
            "email",
            "--external-id",
            external_id,
            "--title",
            title,
            *extra,
        )
        trigger_id = out.split()[0]
        return trigger_id, out.split("thread=", 1)[1].split()[0]

    def trigger(self, trigger_id: str) -> dict:
        return {row["id"]: row for row in self.jsonl("triggers.jsonl")}[trigger_id]

    def deliveries(self) -> list[dict]:
        return self.jsonl("deliveries.jsonl")

    def delivery(self, delivery_id: str) -> dict:
        return {row["id"]: row for row in self.deliveries()}[delivery_id]

    def subscribe(self, demand_id: str, thread_id: str, *extra: str) -> None:
        self.ok("subscribe", demand_id, thread_id, *extra)

    def routed(self) -> tuple[str, str, str, str]:
        """Trigger com dois subscribers -> (trigger, dmd_owner, dmd_watcher, thread)."""
        trigger_id, thread_id = self.add_trigger("mail-1")
        owner = self.add_demand()
        watcher = self.add_demand(area="faculdade", title="Demanda watcher")
        self.subscribe(owner, thread_id)
        self.subscribe(watcher, thread_id, "--role", "watcher")
        self.ok("trigger", "route")
        return trigger_id, owner, watcher, thread_id

    # ---- 1. dois subscribers, duas deliveries ------------------------
    def test_two_subscribers_get_one_delivery_each(self) -> None:
        trigger_id, owner, watcher, _ = self.routed()
        rows = self.deliveries()
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["state"] for row in rows}, {"pending"})
        self.assertEqual(
            {(row["demand_id"], row["role"]) for row in rows},
            {(owner, "owner"), (watcher, "watcher")},
        )
        self.assertEqual({row["attempts"] for row in rows}, {0})
        self.assertEqual({row["lease_epoch"] for row in rows}, {0})
        for row in rows:
            self.assertEqual(row["trigger_id"], trigger_id)
            self.assertIsNone(row["lease_owner"])
            self.assertIsNone(row["lease_until"])
            self.assertIsNone(row["report_id"])
            self.assertEqual(row["created_at"], NOW)
        trigger = self.trigger(trigger_id)
        self.assertEqual(trigger["state"], "routed")
        self.assertEqual(trigger["route"]["by"], "subscription")
        self.assertEqual(
            trigger["route"]["delivery_ids"], [row["id"] for row in rows]
        )

    # ---- 2. ack parcial ----------------------------------------------
    def test_partial_ack_leaves_other_delivery_pending(self) -> None:
        trigger_id, _, _, _ = self.routed()
        first, second = self.deliveries()
        before = self.trigger(trigger_id)

        self.ok("delivery", "claim", first["id"], "--owner", "w1")
        self.ok(
            "delivery", "ack", first["id"],
            "--owner", "w1", "--epoch", "1", "--report", "report-1",
            "--summary", "processado no worker",
        )
        acked = self.delivery(first["id"])
        self.assertEqual(acked["state"], "acked")
        self.assertEqual(acked["report_id"], "report-1")
        self.assertEqual(acked["summary"], "processado no worker")
        self.assertIsNone(acked["lease_owner"])
        self.assertIsNone(acked["lease_until"])
        self.assertEqual(self.delivery(second["id"])["state"], "pending")
        self.assertEqual(self.trigger(trigger_id), before)

    def test_ack_is_idempotent_for_same_report(self) -> None:
        trigger_id, _, _, _ = self.routed()
        first = self.deliveries()[0]
        self.ok("delivery", "claim", first["id"], "--owner", "w1")
        self.ok(
            "delivery", "ack", first["id"],
            "--owner", "w1", "--epoch", "1", "--report", "report-1",
        )
        self.ok(
            "delivery", "ack", first["id"],
            "--owner", "w1", "--epoch", "1", "--report", "report-1",
        )
        self.assertEqual(self.delivery(first["id"])["state"], "acked")
        self.run_fail(
            "delivery", "ack", first["id"],
            "--owner", "w1", "--epoch", "1", "--report", "report-2",
        )
        self.assertEqual(self.trigger(trigger_id)["state"], "routed")

    # ---- 3. lease/epoch ----------------------------------------------
    def test_claim_and_ack_enforce_owner_and_epoch(self) -> None:
        _, _, _, _ = self.routed()
        first = self.deliveries()[0]
        out = self.ok(
            "delivery", "claim", first["id"], "--owner", "w1", "--lease-minutes", "30"
        )
        self.assertIn("epoch=1", out)
        self.assertIn("lease_until=2026-11-01T14:30:00-03:00", out)
        claimed = self.delivery(first["id"])
        self.assertEqual(claimed["state"], "delivered")
        self.assertEqual(claimed["attempts"], 1)
        self.assertEqual(claimed["lease_epoch"], 1)

        self.run_fail(
            "delivery", "ack", first["id"],
            "--owner", "w1", "--epoch", "2", "--report", "report-1",
        )
        self.run_fail(
            "delivery", "ack", first["id"],
            "--owner", "w2", "--epoch", "1", "--report", "report-1",
        )
        self.assertEqual(self.delivery(first["id"])["state"], "delivered")

        self.ok(
            "delivery", "ack", first["id"],
            "--owner", "w1", "--epoch", "1", "--report", "report-1",
        )
        self.assertEqual(self.delivery(first["id"])["state"], "acked")

    def test_claim_rejects_delivered_and_acked(self) -> None:
        _, _, _, _ = self.routed()
        first = self.deliveries()[0]
        self.ok("delivery", "claim", first["id"], "--owner", "w1")
        self.run_fail("delivery", "claim", first["id"], "--owner", "w2")
        self.ok(
            "delivery", "ack", first["id"],
            "--owner", "w1", "--epoch", "1", "--report", "report-1",
        )
        self.run_fail("delivery", "claim", first["id"], "--owner", "w3")

    # ---- 4. fail/retry e expire --------------------------------------
    def test_fail_retry_and_expire_of_lease(self) -> None:
        _, _, _, _ = self.routed()
        first, second = self.deliveries()

        self.ok(
            "delivery", "claim", first["id"], "--owner", "w1", "--lease-minutes", "1"
        )
        self.assertIn("expired=0", self.ok("delivery", "expire"))
        self.assertEqual(self.delivery(first["id"])["state"], "delivered")

        out = self.ok("delivery", "expire", now="2026-11-01T14:05:00-03:00")
        self.assertIn("expired=1", out)
        expired = self.delivery(first["id"])
        self.assertEqual(expired["state"], "failed")
        self.assertEqual(expired["error"], "lease_expired")
        self.assertIsNone(expired["lease_owner"])
        self.assertIsNone(expired["lease_until"])
        self.assertEqual(expired["attempts"], 1)
        self.assertEqual(self.delivery(second["id"])["state"], "pending")

        self.ok("delivery", "retry", first["id"])
        retried = self.delivery(first["id"])
        self.assertEqual(retried["state"], "pending")
        self.assertIsNone(retried["error"])
        self.assertEqual(retried["lease_epoch"], 1)

        out = self.ok("delivery", "claim", first["id"], "--owner", "w2")
        self.assertIn("epoch=2", out)
        self.assertEqual(self.delivery(first["id"])["attempts"], 2)

        self.ok("delivery", "fail", first["id"], "--error", "worker morreu",
                "--owner", "w2", "--epoch", "2")
        failed = self.delivery(first["id"])
        self.assertEqual(failed["state"], "failed")
        self.assertEqual(failed["error"], "worker morreu")
        self.assertEqual(failed["attempts"], 3)
        self.assertIsNone(failed["lease_owner"])

        self.run_fail("delivery", "retry", second["id"])
        self.run_fail("delivery", "fail", first["id"], "--error", "")

    def test_expire_ignores_unexpired_and_pending(self) -> None:
        _, _, _, _ = self.routed()
        first = self.deliveries()[0]
        self.ok(
            "delivery", "claim", first["id"], "--owner", "w1", "--lease-minutes", "60"
        )
        self.assertIn("expired=0", self.ok("delivery", "expire", now="2026-11-01T14:59:59-03:00"))
        self.assertIn("expired=1", self.ok("delivery", "expire", now="2026-11-01T15:00:00-03:00"))

    # ---- 5. demanda terminal não recebe entrega ----------------------
    def test_terminal_demand_is_skipped_by_subscription(self) -> None:
        trigger_id, thread_id = self.add_trigger("mail-term")
        demand_id = self.add_demand()
        self.subscribe(demand_id, thread_id)
        self.ok("transition", demand_id, "routed")
        self.ok("transition", demand_id, "triaged")
        self.ok("transition", demand_id, "done", "--result", "concluída")

        follow_id, follow_thread = self.add_trigger(
            "mail-term-2",
            "--thread-key",
            "email:mail-term",
            "--actor",
            "avisos@sigaa.unifei.edu.br",
            title="SIGAA prazo",
        )
        self.assertEqual(follow_thread, thread_id)
        summary = self.ok("trigger", "route")
        self.assertIn("routed=1", summary)
        trigger = self.trigger(follow_id)
        self.assertEqual(trigger["state"], "routed")
        self.assertEqual(trigger["route"]["by"], "rule")
        self.assertEqual(trigger["route"]["area"], "faculdade")
        self.assertEqual(trigger["route"]["demand_ids"], [])
        self.assertEqual(trigger["route"]["delivery_ids"], [])
        self.assertEqual(self.deliveries(), [])
        stale = self.trigger(trigger_id)
        self.assertEqual(stale["state"], "candidate")
        self.assertEqual(stale["route"]["delivery_ids"], [])

    def test_dropped_demand_also_skips_delivery(self) -> None:
        _, thread_id = self.add_trigger("mail-drop")
        demand_id = self.add_demand()
        self.subscribe(demand_id, thread_id)
        self.ok("transition", demand_id, "routed")
        self.ok("transition", demand_id, "triaged")
        self.ok("transition", demand_id, "dropped", "--reason", "não é mais relevante")
        self.ok("trigger", "route")
        self.assertEqual(self.deliveries(), [])

    # ---- 5b. demanda terminal desativa subscription e delivery -------
    def test_done_cancels_subscription_and_skips_delivered(self) -> None:
        _, owner, watcher, thread_id = self.routed()
        later = "2026-11-01T15:00:00-03:00"
        owner_delivery = next(
            row for row in self.deliveries() if row["demand_id"] == owner
        )
        self.ok("delivery", "claim", owner_delivery["id"], "--owner", "w1")
        self.ok("transition", owner, "routed")
        self.ok("transition", owner, "triaged")
        self.ok("transition", owner, "done", "--result", "concluída", now=later)

        subscriptions = {
            row["demand_id"]: row for row in self.jsonl("subscriptions.jsonl")
        }
        self.assertFalse(subscriptions[owner]["active"])
        self.assertEqual(subscriptions[owner]["updated_at"], later)
        self.assertTrue(subscriptions[watcher]["active"])

        deliveries = {row["demand_id"]: row for row in self.deliveries()}
        self.assertEqual(deliveries[owner]["state"], "skipped")
        self.assertEqual(deliveries[owner]["error"], "demand_terminal")
        self.assertEqual(deliveries[owner]["updated_at"], later)
        self.assertEqual(deliveries[watcher]["state"], "pending")

        out = self.ok("doctor")
        self.assertNotIn("subscription ativa para demanda terminal", out)
        self.assertNotIn("delivery", out)

    def test_dropped_cancels_subscription_and_skips_pending(self) -> None:
        _, owner, watcher, thread_id = self.routed()
        later = "2026-11-01T15:00:00-03:00"
        self.ok("transition", owner, "routed")
        self.ok("transition", owner, "triaged")
        self.ok(
            "transition",
            owner,
            "dropped",
            "--reason",
            "não é mais relevante",
            now=later,
        )

        subscriptions = {
            row["demand_id"]: row for row in self.jsonl("subscriptions.jsonl")
        }
        self.assertFalse(subscriptions[owner]["active"])
        self.assertTrue(subscriptions[watcher]["active"])

        deliveries = {row["demand_id"]: row for row in self.deliveries()}
        self.assertEqual(deliveries[owner]["state"], "skipped")
        self.assertEqual(deliveries[owner]["error"], "demand_terminal")
        self.assertEqual(deliveries[owner]["updated_at"], later)
        self.assertEqual(deliveries[watcher]["state"], "pending")

    # ---- 6. dry-run não cria delivery --------------------------------
    def test_dry_run_creates_no_delivery(self) -> None:
        trigger_id, thread_id = self.add_trigger("mail-dry")
        demand_id = self.add_demand()
        self.subscribe(demand_id, thread_id)
        out = self.ok("trigger", "route", "--dry-run")
        self.assertIn(f"demands={demand_id}", out)
        self.assertIn("dry-run", out)
        self.assertEqual(self.deliveries(), [])
        self.assertEqual(self.trigger(trigger_id)["state"], "new")

    # ---- 7. rebuild preserva deliveries ------------------------------
    def test_rebuild_preserves_deliveries(self) -> None:
        trigger_id, _, _, _ = self.routed()
        first = self.deliveries()[0]
        self.ok("delivery", "claim", first["id"], "--owner", "w1")
        before = self.deliveries()
        self.ok("rebuild")
        self.assertEqual(self.deliveries(), before)
        self.assertEqual(self.trigger(trigger_id)["route"]["delivery_ids"],
                         [row["id"] for row in before])

    # ---- 8. doctor ---------------------------------------------------
    def test_doctor_flags_delivery_problems(self) -> None:
        thread_id = "thr-20261101-0001"
        self.write_jsonl("threads.jsonl", [{"id": thread_id, "state": "open"}])
        self.write_jsonl(
            "demands.jsonl",
            [
                {"id": "dmd-20261101-0001", "area": "projetos", "state": "done",
                 "lane": "fast", "title": "Feita", "updated_at": NOW,
                 "lease_epoch": 0, "revision": 1},
                {"id": "dmd-20261101-0002", "area": "projetos", "state": "ready",
                 "lane": "fast", "title": "Ativa", "updated_at": NOW,
                 "lease_epoch": 0, "revision": 1},
            ],
        )
        self.write_jsonl(
            "subscriptions.jsonl",
            [
                {"thread_id": thread_id, "demand_id": "dmd-20261101-0001",
                 "role": "owner", "active": True, "created_at": NOW},
            ],
        )
        self.write_jsonl(
            "triggers.jsonl",
            [
                {"id": "trg-20261101-0001", "source": "email",
                 "external_id": "mail-x", "thread_id": thread_id,
                 "state": "routed", "received_at": NOW, "route": {
                     "by": "subscription", "demand_ids": ["dmd-20261101-0002"],
                     "delivery_ids": [],
                 }},
            ],
        )
        self.write_jsonl(
            "deliveries.jsonl",
            [
                {"id": "dlv-20261101-0001", "trigger_id": "trg-20261101-0001",
                 "demand_id": "dmd-20261101-0001", "role": "owner",
                 "state": "acked", "attempts": 1, "lease_owner": None,
                 "lease_until": None, "lease_epoch": 1, "report_id": None,
                 "created_at": NOW, "updated_at": NOW, "error": None},
                {"id": "dlv-20261101-0002", "trigger_id": "trg-20261101-0001",
                 "demand_id": "dmd-20261101-0001", "role": "watcher",
                 "state": "pending", "attempts": 0,
                 "lease_owner": "ghost", "lease_until": NOW,
                 "lease_epoch": 1, "report_id": None,
                 "created_at": NOW, "updated_at": NOW, "error": None},
                {"id": "dlv-20261101-0003", "trigger_id": "trg-20261101-9999",
                 "demand_id": "dmd-20261101-9999", "role": "owner",
                 "state": "delivered", "attempts": 1, "lease_owner": None,
                 "lease_until": None, "lease_epoch": 1, "report_id": None,
                 "created_at": NOW, "updated_at": NOW, "error": None},
            ],
        )
        proc = self.run_fail("doctor", code=1)
        out = proc.stdout
        self.assertIn("delivery sem trigger: dlv-20261101-0003", out)
        self.assertIn("delivery sem demanda: dlv-20261101-0003", out)
        self.assertIn(
            "delivery duplicada (trg-20261101-0001, dmd-20261101-0001): "
            "dlv-20261101-0001 e dlv-20261101-0002",
            out,
        )
        self.assertIn("delivery acked sem report_id: dlv-20261101-0001", out)
        self.assertIn("delivery delivered sem lease: dlv-20261101-0003", out)
        self.assertIn("delivery pending com lease preenchido: dlv-20261101-0002", out)
        self.assertIn("subscription ativa para demanda terminal: dmd-20261101-0001", out)
        self.assertIn(
            "trigger routed por subscription sem delivery: trg-20261101-0001", out
        )

    def test_doctor_accepts_consistent_deliveries(self) -> None:
        self.routed()
        self.assertEqual(self.ok("doctor").strip(), "doctor ok")

    # ---- 9. rotear de novo não duplica -------------------------------
    def test_reroute_does_not_duplicate_deliveries(self) -> None:
        trigger_id, _, _, _ = self.routed()
        before = self.deliveries()
        out = self.ok("trigger", "route")
        self.assertIn("routed=0", out)
        after = self.deliveries()
        self.assertEqual(after, before)
        self.assertEqual(len(after), 2)
        self.assertEqual(
            self.trigger(trigger_id)["route"]["delivery_ids"],
            [row["id"] for row in after],
        )

    # ---- list --------------------------------------------------------
    def test_list_filters_and_formats(self) -> None:
        trigger_id, owner, watcher, _ = self.routed()
        first = self.deliveries()[0]
        self.ok("delivery", "claim", first["id"], "--owner", "w1")
        lines = self.ok("delivery", "list").splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn(f"{first['id']} | delivered | {trigger_id} | {first['demand_id']}", lines[0])
        self.assertIn("| w1/1 | -", lines[0])

        pending = self.ok("delivery", "list", "--state", "pending").splitlines()
        self.assertEqual(len(pending), 1)
        remaining = watcher if first["demand_id"] == owner else owner
        remaining_role = "watcher" if remaining == watcher else "owner"
        self.assertIn(remaining, pending[0])
        self.assertIn(f"| {remaining_role} |", pending[0])

        by_demand = self.ok("delivery", "list", "--demand", watcher).splitlines()
        self.assertEqual(len(by_demand), 1)
        self.assertIn(watcher, by_demand[0])

        rows = json.loads(self.ok("delivery", "list", "--json"))
        self.assertEqual(len(rows), 2)


if __name__ == "__main__":
    unittest.main()
