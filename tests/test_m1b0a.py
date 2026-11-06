"""Testes do M1b.0a: triggers, threads, subscriptions e fast path."""

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

# `ignore` é opt-in: o default não descarta remetente (DROP-01).
IGNORE_RULES = {
    "version": 1,
    "default": "candidate",
    "rules": [
        {
            "name": "ignore-noreply",
            "match": {
                "source": "email",
                "from_regex": "(?i)(no-?reply|nao-?responda)",
            },
            "action": "ignore",
        }
    ],
}


class M1b0aTestCase(unittest.TestCase):
    def setUp(self) -> None:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.home = Path(tempfile.mkdtemp(prefix="atlas-m1b0a-", dir=TMP_ROOT))
        self.env = dict(os.environ, ATLAS_HOME=str(self.home))
        self.ok("init")

    def tearDown(self) -> None:
        shutil.rmtree(self.home, ignore_errors=True)

    # ---- helpers ------------------------------------------------------
    def run_atlas(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(BIN), "--now", NOW, *args],
            capture_output=True,
            text=True,
            env=self.env,
            check=False,
        )

    def ok(self, *args: str) -> str:
        proc = self.run_atlas(*args)
        self.assertEqual(proc.returncode, 0, f"stdout={proc.stdout}\nstderr={proc.stderr}")
        return proc.stdout

    def run_fail(self, *args: str, code: int = 2) -> subprocess.CompletedProcess:
        proc = self.run_atlas(*args)
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

    def add_demand(self) -> str:
        out = self.ok("add", "--area", "projetos", "--title", "Demanda teste")
        return out.split()[0]

    def add_trigger(self, external_id: str, *extra: str, title: str = "Evento teste") -> str:
        return self.ok(
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

    def thread_of(self, stdout: str) -> str:
        return stdout.split("thread=", 1)[1].split()[0]

    def trigger(self, trigger_id: str) -> dict:
        return {row["id"]: row for row in self.jsonl("triggers.jsonl")}[trigger_id]

    def write_rules(self, rules: dict) -> None:
        path = self.home / "router" / "rules.v1.json"
        path.write_text(json.dumps(rules, ensure_ascii=False), encoding="utf-8")

    def route(self, *extra: str) -> str:
        return self.ok("trigger", "route", *extra)

    # ---- 1. add idempotente ------------------------------------------
    def test_add_is_idempotent_per_source_and_external_id(self) -> None:
        first = self.add_trigger("imap:1")
        second = self.add_trigger("imap:1")
        self.assertEqual(first, second)
        self.assertEqual(len(self.jsonl("triggers.jsonl")), 1)
        self.assertEqual(len(self.jsonl("threads.jsonl")), 1)
        trigger = self.jsonl("triggers.jsonl")[0]
        self.assertEqual(trigger["state"], "new")
        self.assertTrue(trigger["untrusted"])
        self.assertEqual(trigger["thread_id"], self.thread_of(first))

    # ---- 2. regra ignore ---------------------------------------------
    def test_ignore_rule_marks_trigger_ignored(self) -> None:
        self.write_rules(IGNORE_RULES)
        out = self.add_trigger("mail-1", "--actor", "no-reply@example.com")
        trigger_id = out.split()[0]
        summary = self.route()
        self.assertIn("routed=0 ignored=1 candidate=0", summary)
        trigger = self.trigger(trigger_id)
        self.assertEqual(trigger["state"], "ignored")
        self.assertEqual(trigger["route"]["by"], "rule")
        self.assertEqual(trigger["route"]["rule"], "ignore-noreply")
        self.assertIsNone(trigger["route"]["area"])
        self.assertIsNone(trigger["route"]["demand_id"])
        self.assertEqual(trigger["route"]["demand_ids"], [])
        self.assertIsNone(trigger["route"]["confidence"])

    def test_noreply_finance_email_is_routed_not_ignored(self) -> None:
        out = self.add_trigger(
            "mail-noreply-fin",
            "--actor",
            "no-reply@nubank.com.br",
            title="Fatura Nubank",
        )
        self.route()
        trigger = self.trigger(out.split()[0])
        self.assertEqual(trigger["state"], "routed")
        self.assertEqual(trigger["route"]["area"], "financas")
        self.assertEqual(trigger["route"]["rule"], "financas-nubank")

    def test_noreply_sigaa_email_is_routed_not_ignored(self) -> None:
        out = self.add_trigger(
            "mail-noreply-sigaa",
            "--actor",
            "no-reply@sigaa.unifei.edu.br",
            title="SIGAA prazo",
        )
        self.route()
        trigger = self.trigger(out.split()[0])
        self.assertEqual(trigger["state"], "routed")
        self.assertEqual(trigger["route"]["area"], "faculdade")
        self.assertEqual(trigger["route"]["rule"], "faculdade-sigaa")

    def test_generic_noreply_is_candidate(self) -> None:
        out = self.add_trigger(
            "mail-noreply-gen",
            "--actor",
            "no-reply@example.com",
            title="Newsletter semanal",
        )
        self.route()
        trigger = self.trigger(out.split()[0])
        self.assertEqual(trigger["state"], "candidate")
        self.assertIsNone(trigger["route"]["rule"])

    # ---- 3. regra route ----------------------------------------------
    def test_route_rule_records_area(self) -> None:
        out = self.add_trigger("mail-2", "--actor", "avisos@sigaa.unifei.edu.br")
        trigger_id = out.split()[0]
        summary = self.route()
        self.assertIn("routed=1", summary)
        trigger = self.trigger(trigger_id)
        self.assertEqual(trigger["state"], "routed")
        self.assertEqual(trigger["route"]["by"], "rule")
        self.assertEqual(trigger["route"]["area"], "faculdade")
        self.assertEqual(trigger["route"]["rule"], "faculdade-sigaa")

    def test_unifei_reviewer_about_wsf_routes_to_projetos(self) -> None:
        out = self.add_trigger(
            "mail-unifei",
            "--actor",
            "revisor@unifei.edu.br",
            title="Comentarios WSF",
        )
        trigger_id = out.split()[0]
        self.route()
        trigger = self.trigger(trigger_id)
        self.assertEqual(trigger["state"], "routed")
        self.assertEqual(trigger["route"]["area"], "projetos")
        self.assertEqual(trigger["route"]["rule"], "projetos-fapemig")

    def test_older_follow_up_keeps_latest_thread_timestamp(self) -> None:
        self.add_trigger(
            "mail-12",
            "--thread-key",
            "email:<CAF@z>",
            "--received-at",
            "2026-11-01T12:00:00-03:00",
        )
        self.add_trigger(
            "mail-13",
            "--thread-key",
            "email:<CAF@z>",
            "--received-at",
            "2026-10-30T09:00:00-03:00",
        )
        thread = self.jsonl("threads.jsonl")[0]
        self.assertEqual(thread["last_trigger_at"], "2026-11-01T12:00:00-03:00")

    # ---- 4. área inválida nas regras ---------------------------------
    def test_invalid_area_in_rules_fails_without_touching_trigger(self) -> None:
        out = self.add_trigger("mail-3")
        trigger_id = out.split()[0]
        self.write_rules(
            {
                "version": 1,
                "default": "candidate",
                "rules": [
                    {
                        "name": "area-ruim",
                        "match": {"source": "email"},
                        "action": "route",
                        "area": "inexistente",
                    }
                ],
            }
        )
        proc = self.run_fail("trigger", "route")
        self.assertIn("área inválida", proc.stderr)
        self.assertEqual(self.trigger(trigger_id)["state"], "new")

    # ---- 5. subscription vence a regra -------------------------------
    def test_active_subscription_takes_precedence_over_ignoring_rule(self) -> None:
        out = self.add_trigger("mail-4", "--actor", "no-reply@example.com")
        trigger_id = out.split()[0]
        thread_id = self.thread_of(out)
        demand_id = self.add_demand()
        self.ok("subscribe", demand_id, thread_id)
        self.route()
        trigger = self.trigger(trigger_id)
        self.assertEqual(trigger["state"], "routed")
        self.assertEqual(trigger["route"]["by"], "subscription")
        self.assertEqual(trigger["route"]["demand_ids"], [demand_id])

    # ---- 6. muitos-para-muitos ---------------------------------------
    def test_two_demands_receive_the_same_thread_trigger(self) -> None:
        out = self.add_trigger("mail-5")
        trigger_id = out.split()[0]
        thread_id = self.thread_of(out)
        first = self.add_demand()
        second = self.add_demand()
        self.ok("subscribe", first, thread_id)
        self.ok("subscribe", second, thread_id)
        self.route()
        self.assertEqual(
            self.trigger(trigger_id)["route"]["demand_ids"], sorted([first, second])
        )

    # ---- 7. mesma thread_key, mesma thread ---------------------------
    def test_follow_up_with_same_thread_key_reuses_thread(self) -> None:
        first = self.add_trigger("mail-6", "--thread-key", "email:<CAF@x>")
        second = self.add_trigger("mail-7", "--thread-key", "email:<CAF@x>")
        self.assertEqual(self.thread_of(first), self.thread_of(second))
        self.assertEqual(len(self.jsonl("threads.jsonl")), 1)

    # ---- 8. unsubscribe volta à regra --------------------------------
    def test_unsubscribe_disables_pass_through(self) -> None:
        self.write_rules(IGNORE_RULES)
        out = self.add_trigger("mail-8", "--thread-key", "email:<CAF@y>")
        thread_id = self.thread_of(out)
        demand_id = self.add_demand()
        self.ok("subscribe", demand_id, thread_id)
        self.ok("unsubscribe", demand_id, thread_id)
        subscription = self.jsonl("subscriptions.jsonl")[0]
        self.assertFalse(subscription["active"])

        follow = self.add_trigger(
            "mail-9",
            "--thread-key",
            "email:<CAF@y>",
            "--actor",
            "no-reply@example.com",
        )
        follow_id = follow.split()[0]
        self.route()
        trigger = self.trigger(follow_id)
        self.assertEqual(trigger["state"], "ignored")
        self.assertEqual(trigger["route"]["by"], "rule")
        self.assertEqual(trigger["route"]["rule"], "ignore-noreply")

    def test_doctor_accepts_rule_ignored_trigger(self) -> None:
        self.write_rules(IGNORE_RULES)
        self.add_trigger("mail-14", "--actor", "no-reply@example.com")
        self.route()
        self.assertEqual(self.ok("doctor").strip().splitlines()[-1], "doctor ok")

    # ---- 9. rebuild preserva -----------------------------------------
    def test_rebuild_preserves_triggers_threads_and_subscriptions(self) -> None:
        out = self.add_trigger("mail-10")
        thread_id = self.thread_of(out)
        demand_id = self.add_demand()
        self.ok("subscribe", demand_id, thread_id)
        self.route()
        before = {
            name: self.jsonl(name)
            for name in ("triggers.jsonl", "threads.jsonl", "subscriptions.jsonl")
        }
        self.ok("rebuild")
        after = {
            name: self.jsonl(name)
            for name in ("triggers.jsonl", "threads.jsonl", "subscriptions.jsonl")
        }
        self.assertEqual(before, after)
        self.assertEqual(len(after["triggers.jsonl"]), 1)
        self.assertEqual(after["triggers.jsonl"][0]["state"], "routed")

    # ---- 10. doctor acusa subscription órfã --------------------------
    def test_doctor_reports_orphan_subscription(self) -> None:
        self.write_jsonl(
            "subscriptions.jsonl",
            [
                {
                    "thread_id": "thr-20261101-0001",
                    "demand_id": "dmd-20261101-9999",
                    "role": "owner",
                    "active": True,
                    "created_at": "2026-11-01T14:00:00-03:00",
                }
            ],
        )
        proc = self.run_fail("doctor", code=1)
        self.assertIn("subscription ativa para thread inexistente", proc.stdout)
        self.assertIn("subscription ativa para demanda inexistente", proc.stdout)

    # ---- 11. transição ilegal ----------------------------------------
    def test_illegal_trigger_transition_is_rejected(self) -> None:
        out = self.add_trigger("mail-11")
        trigger_id = out.split()[0]
        self.run_fail("trigger", "transition", trigger_id, "processed")
        self.assertEqual(self.trigger(trigger_id)["state"], "new")


if __name__ == "__main__":
    unittest.main()
