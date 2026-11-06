"""Testes do M1b.0c: watchdog determinístico (liveness/progresso)."""

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


class M1b0cTestCase(unittest.TestCase):
    def setUp(self) -> None:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.home = Path(tempfile.mkdtemp(prefix="atlas-m1b0c-", dir=TMP_ROOT))
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

    def ok(self, *args: str) -> str:
        proc = self.run_atlas(*args)
        self.assertEqual(proc.returncode, 0, f"stdout={proc.stdout}\nstderr={proc.stderr}")
        return proc.stdout

    def jsonl(self, name: str) -> list[dict]:
        path = self.home / "state" / name
        if not path.exists():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def write_jsonl(self, name: str, rows: list[dict]) -> None:
        path = self.home / "state" / name
        path.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
            encoding="utf-8",
        )

    def write_text(self, name: str, text: str) -> Path:
        path = self.home / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def add_ready(self, title: str = "Demanda watchdog") -> str:
        demand_id = self.ok("add", "--area", "projetos", "--title", title).split()[0]
        for state in ("routed", "triaged", "planned", "ready"):
            self.ok("transition", demand_id, state)
        return demand_id

    def mutate_demand(self, demand_id: str, **changes) -> dict:
        rows = self.jsonl("demands.jsonl")
        for row in rows:
            if row["id"] == demand_id:
                row.update(changes)
                self.write_jsonl("demands.jsonl", rows)
                return row
        raise AssertionError(f"demanda inexistente: {demand_id}")

    def watchdog(self, *args: str, now: str = NOW) -> subprocess.CompletedProcess:
        return self.run_atlas("watchdog", *args, now=now)

    def codes(self, items: list[dict]) -> list[str]:
        return [item["code"] for item in items]

    def check_json(self, *args: str, now: str = NOW) -> list[dict]:
        proc = self.watchdog("--json", *args, now=now)
        return json.loads(proc.stdout)

    # ---- 1. lease vencido + repair ------------------------------------
    def test_lease_expired_and_repair(self) -> None:
        demand_id = self.add_ready()
        self.mutate_demand(
            demand_id,
            state="running",
            owner="agente",
            lease_until="2026-11-01T13:55:00-03:00",
            lease_epoch=1,
        )
        items = self.check_json()
        self.assertIn("lease_expired", self.codes(items))
        self.assertFalse(any(item["severity"] == "critical" for item in items))

        proc = self.watchdog("--repair")
        self.assertEqual(proc.returncode, 0, proc.stderr)

        demand = self.jsonl("demands.jsonl")[0]
        self.assertEqual(demand["state"], "ready")
        self.assertIsNone(demand["lease_until"])
        self.assertFalse(any(item["code"] == "lease_expired" for item in self.check_json()))

        events = self.jsonl("events.jsonl")
        repair_event = events[-1]
        self.assertEqual(repair_event["demand_id"], demand_id)
        self.assertEqual((repair_event["from"], repair_event["to"]), ("running", "ready"))
        self.assertEqual(repair_event["actor"], "system")
        self.assertEqual(repair_event["reason"], "lease_expired")
        self.assertTrue((self.home / "demands" / f"{demand_id}.md").exists())

    # ---- 2. running sem progresso --------------------------------------
    def test_stale_running_without_recent_event(self) -> None:
        demand_id = self.add_ready()
        self.mutate_demand(
            demand_id,
            state="running",
            owner="agente",
            lease_until="2026-11-01T15:00:00-03:00",
            lease_epoch=1,
        )
        self.write_jsonl(
            "events.jsonl",
            [
                {
                    "seq": 1,
                    "ts": "2026-11-01T13:30:00-03:00",
                    "demand_id": demand_id,
                    "from": "ready",
                    "to": "running",
                    "actor": "cli",
                    "reason": "",
                    "run_id": "",
                    "lease_epoch": 1,
                    "tx_id": "tx-teste",
                }
            ],
        )
        items = self.check_json()
        self.assertIn("stale_running", self.codes(items))
        self.assertNotIn("lease_expired", self.codes(items))

        # evento recente (5 min) silencia o alerta
        rows = self.jsonl("events.jsonl")
        rows[-1]["ts"] = "2026-11-01T13:55:00-03:00"
        self.write_jsonl("events.jsonl", rows)
        self.assertNotIn("stale_running", self.codes(self.check_json()))

    # ---- 3. notificação presa ------------------------------------------
    def test_notification_stuck(self) -> None:
        self.write_jsonl(
            "notifications.jsonl",
            [
                {
                    "id": "ntf-20261101-0001",
                    "digest_id": "dig-20261101-0001",
                    "created_at": "2026-11-01T10:00:00-03:00",
                    "status": "pending",
                    "items": [],
                }
            ],
        )
        items = self.check_json()
        stuck = [item for item in items if item["code"] == "notification_stuck"]
        self.assertEqual(len(stuck), 1)
        self.assertEqual(stuck[0]["severity"], "warn")
        self.assertEqual(stuck[0]["subject"], "ntf-20261101-0001")

    # ---- 4. backlog de triggers ----------------------------------------
    def test_candidate_backlog_is_critical_above_100(self) -> None:
        self.write_jsonl("threads.jsonl", [{"id": "thr-20261101-0001", "state": "open"}])
        rows = [
            {
                "id": f"trg-20261101-{index:04d}",
                "source": "email",
                "external_id": f"msg-{index}",
                "thread_id": "thr-20261101-0001",
                "state": "candidate",
                "received_at": "2026-10-30T09:00:00-03:00",
                "route": {},
            }
            for index in range(1, 102)
        ]
        self.write_jsonl("triggers.jsonl", rows)

        items = self.check_json()
        backlog = [item for item in items if item["code"] == "candidate_backlog"]
        self.assertEqual(len(backlog), 1)
        self.assertEqual(backlog[0]["severity"], "critical")
        self.assertIn("101", backlog[0]["message"])
        self.assertEqual(self.watchdog().returncode, 1)

    # ---- 5. digest parado ----------------------------------------------
    def test_digest_stale_with_active_demand(self) -> None:
        self.add_ready()
        cursor = self.home / "state" / "cursor.json"
        cursor.write_text(
            json.dumps({"last_digest_at": "2026-11-01T11:00:00-03:00"}),
            encoding="utf-8",
        )
        items = self.check_json()
        self.assertIn("digest_stale", self.codes(items))

        cursor.write_text(
            json.dumps({"last_digest_at": "2026-11-01T13:30:00-03:00"}),
            encoding="utf-8",
        )
        self.assertNotIn("digest_stale", self.codes(self.check_json()))

    # ---- 6. doctor crítico ---------------------------------------------
    def test_doctor_problem_is_critical_and_exit_1(self) -> None:
        demand_id = self.add_ready()
        rows = self.jsonl("demands.jsonl")
        for row in rows:
            if row["id"] == demand_id:
                row.pop("lease_epoch", None)
        self.write_jsonl("demands.jsonl", rows)

        proc = self.watchdog("--json")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        items = json.loads(proc.stdout)
        doctor_items = [item for item in items if item["code"] == "doctor"]
        self.assertTrue(doctor_items)
        self.assertTrue(all(item["severity"] == "critical" for item in doctor_items))
        self.assertTrue(any(demand_id in item["message"] for item in doctor_items))

    # ---- 7. BULLETIN ---------------------------------------------------
    def test_bulletin_has_health_section_before_digests(self) -> None:
        self.add_ready()
        text = self.ok("bulletin")
        self.assertIn("## SAÚDE", text)
        self.assertLess(text.index("## SAÚDE"), text.index("## ÚLTIMOS DIGESTS"))
        self.assertIn("[warn] digest_stale cursor:", text)
        self.assertEqual(text, self.ok("bulletin"))

    def test_bulletin_health_is_ok_when_clean(self) -> None:
        text = self.ok("bulletin")
        self.assertIn("## SAÚDE\n- ok\n", text)

    # ---- 8. JSON determinístico ----------------------------------------
    def test_json_output_is_deterministic(self) -> None:
        demand_id = self.add_ready()
        self.mutate_demand(
            demand_id,
            state="running",
            owner="agente",
            lease_until="2026-11-01T13:55:00-03:00",
            lease_epoch=1,
        )
        first = self.watchdog("--json")
        second = self.watchdog("--json")
        self.assertEqual(first.returncode, 0)
        self.assertEqual(first.stdout, second.stdout)
        items = json.loads(first.stdout)
        self.assertTrue(items)
        for item in items:
            self.assertEqual(
                sorted(item), ["code", "message", "severity", "subject"]
            )

    # ---- 9. demanda ativa sem lane -------------------------------------
    def test_demand_without_lane(self) -> None:
        demand_id = self.add_ready()
        self.assertNotIn("demand_no_lane", self.codes(self.check_json()))

        self.mutate_demand(demand_id, lane=None)
        lane_items = [
            item
            for item in self.check_json()
            if item["code"] == "demand_no_lane"
        ]
        self.assertEqual(len(lane_items), 1)
        self.assertEqual(lane_items[0]["severity"], "warn")
        self.assertEqual(lane_items[0]["subject"], demand_id)
        self.assertEqual(lane_items[0]["message"], "demanda ativa sem lane")

    # ---- 10. nó de KB stub ---------------------------------------------
    def add_kb_node(self, title: str, summary: str = "resumo") -> str:
        if not any(row["id"] == "projetos" for row in self.jsonl("kb.jsonl")):
            self.ok(
                "kb", "add", "--parent", "", "--kind", "area", "--title", "Projetos",
                "--summary", "área de projetos",
            )
        node_id = self.ok(
            "kb", "add", "--parent", "projetos", "--kind", "nota", "--title", title,
            "--summary", summary,
        ).strip()
        return node_id

    def clear_kb_summary(self, node_id: str) -> None:
        rows = self.jsonl("kb.jsonl")
        for row in rows:
            if row["id"] == node_id:
                row["summary"] = ""
        self.write_jsonl("kb.jsonl", rows)

    def test_kb_stub_without_summary(self) -> None:
        node_id = self.add_kb_node("Nota com resumo")
        self.assertNotIn("kb_stub", self.codes(self.check_json()))

        self.clear_kb_summary(node_id)
        stub_items = [item for item in self.check_json() if item["code"] == "kb_stub"]
        self.assertEqual(len(stub_items), 1)
        self.assertEqual(stub_items[0]["severity"], "warn")
        self.assertEqual(stub_items[0]["subject"], node_id)
        self.assertEqual(stub_items[0]["message"], "nó de KB sem summary")
        self.assertEqual(self.watchdog().returncode, 0)

    def test_kb_stubs_are_capped_and_summarized(self) -> None:
        for index in range(12):
            node_id = self.add_kb_node(f"Nota {index:02d}")
            self.clear_kb_summary(node_id)
        stub_items = [item for item in self.check_json() if item["code"] == "kb_stub"]
        self.assertEqual(len(stub_items), 11)
        self.assertEqual(
            [item["subject"] for item in stub_items[:10]],
            sorted(item["subject"] for item in stub_items[:10]),
        )
        self.assertEqual(stub_items[-1]["subject"], "kb")
        self.assertEqual(stub_items[-1]["message"], "12 nó(s) de KB sem summary")

    # ---- 11. BULLETIN com a saúde antiga --------------------------------
    def test_bulletin_reports_lane_and_kb_health(self) -> None:
        demand_id = self.add_ready()
        self.mutate_demand(demand_id, lane="")
        node_id = self.add_kb_node("Nota stub")
        self.clear_kb_summary(node_id)

        text = self.ok("bulletin")
        self.assertIn("## SAÚDE", text)
        self.assertIn(f"[warn] demand_no_lane {demand_id}: demanda ativa sem lane", text)
        self.assertIn(f"[warn] kb_stub {node_id}: nó de KB sem summary", text)
        self.assertEqual(text, self.ok("bulletin"))

    def test_json_is_empty_and_exit_zero_when_clean(self) -> None:
        proc = self.watchdog("--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), [])

    # ---- extra: pausa via control.json e disco -----------------------
    def test_paused_is_reported(self) -> None:
        self.ok("pause", "--reason", "manutenção")
        items = self.check_json()
        paused = next(item for item in items if item["code"] == "paused")
        self.assertEqual(paused["severity"], "warn")
        self.assertFalse(any(item["severity"] == "critical" for item in items))
        self.ok("resume")
        self.assertNotIn("paused", self.codes(self.check_json()))


if __name__ == "__main__":
    unittest.main()
