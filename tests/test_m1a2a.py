"""Testes do M1a.2a: digest com outbox, sender injetado e retry."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from atlas_cli import digest, outbox  # noqa: E402
from atlas_cli.cli import main as atlas_main  # noqa: E402
from atlas_cli.store import Store  # noqa: E402

TMP_ROOT = REPO / "tests" / ".tmp"
NOW = "2026-11-01T14:00:00-03:00"
LATER = "2026-11-01T14:05:00-03:00"


class M1a2aTestCase(unittest.TestCase):
    def setUp(self) -> None:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.home = Path(tempfile.mkdtemp(prefix="atlas-m1a2a-", dir=TMP_ROOT))
        self._env = os.environ.get("ATLAS_HOME")
        os.environ["ATLAS_HOME"] = str(self.home)
        self.assertEqual(atlas_main(["--now", NOW, "init"]), 0)

    def tearDown(self) -> None:
        if self._env is None:
            os.environ.pop("ATLAS_HOME", None)
        else:
            os.environ["ATLAS_HOME"] = self._env
        shutil.rmtree(self.home, ignore_errors=True)

    # ---- helpers ------------------------------------------------------
    def atlas(self, *argv: str, now: str = NOW) -> str:
        self.assertEqual(atlas_main(["--now", now, *argv]), 0)
        return ""

    def jsonl(self, name: str) -> list[dict]:
        path = self.home / "state" / name
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def cursor(self) -> dict:
        return json.loads(
            (self.home / "state" / "cursor.json").read_text(encoding="utf-8")
        )

    def add(self, *extra: str, now: str = NOW) -> str:
        before = len(self.jsonl("demands.jsonl"))
        self.atlas("add", "--area", "projetos", "--title", "Demanda teste", *extra, now=now)
        rows = self.jsonl("demands.jsonl")
        self.assertEqual(len(rows), before + 1)
        return rows[-1]["id"]

    def add_many(self, count: int) -> list[str]:
        ids = []
        for index in range(count):
            self.atlas(
                "add",
                "--area",
                "projetos",
                "--title",
                f"Demanda {index:02d}",
                "--importance",
                "3",
                "--urgency",
                "1",
            )
            ids.append(self.jsonl("demands.jsonl")[-1]["id"])
        return ids

    def demand(self, demand_id: str) -> dict:
        return {row["id"]: row for row in self.jsonl("demands.jsonl")}[demand_id]

    def digest(self, now: str = NOW, **kwargs):
        sender = kwargs.pop("sender", None)
        return digest.run(Store(now=now), sender=sender, **kwargs)

    def sent_sender(self, log: list[str] | None = None, message_id: str = "msg-1"):
        def sender(message: str, payload: dict) -> str:
            if log is not None:
                log.append(message)
            return message_id

        return sender

    # ---- 1. falha no sender ------------------------------------------
    def test_sender_failure_keeps_cursor_and_retry_reuses_digest_id(self) -> None:
        demand_id = self.add("--importance", "5", "--urgency", "4")

        def boom(message: str, payload: dict) -> str:
            raise RuntimeError("smtp fora do ar")

        code, message = self.digest(sender=boom)
        self.assertEqual(code, 1)
        self.assertIn("falha no envio", message)

        notifications = self.jsonl("notifications.jsonl")
        self.assertEqual(len(notifications), 1)
        failed = notifications[0]
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["attempts"], 1)
        self.assertIn("smtp fora do ar", failed["error"])
        self.assertIsNone(self.cursor()["last_digest_at"])
        self.assertIsNone(self.demand(demand_id)["reported_at"])

        code, message = outbox.run(
            Store(now=LATER), retry=True, sender=self.sent_sender()
        )
        self.assertEqual(code, 0, message)
        retried = self.jsonl("notifications.jsonl")[-1]
        self.assertEqual(retried["status"], "sent")
        self.assertEqual(retried["digest_id"], failed["digest_id"])
        self.assertEqual(retried["payload_hash"], failed["payload_hash"])
        self.assertEqual(retried["id"], failed["id"])
        self.assertEqual(retried["attempts"], 2)
        self.assertEqual(retried["message_id"], "msg-1")

    # ---- 2. sucesso ---------------------------------------------------
    def test_sender_success_sets_sent_cursor_and_reported_at(self) -> None:
        demand_id = self.add("--importance", "5", "--urgency", "4")
        code, message = self.digest(sender=self.sent_sender())
        self.assertEqual(code, 0, message)

        notification = self.jsonl("notifications.jsonl")[-1]
        self.assertEqual(notification["status"], "sent")
        self.assertEqual(notification["attempts"], 1)
        self.assertEqual(notification["message_id"], "msg-1")
        self.assertEqual(notification["sent_at"], NOW)
        self.assertIsNotNone(notification["payload_hash"])

        demand = self.demand(demand_id)
        self.assertEqual(demand["reported_at"], NOW)
        self.assertEqual(demand["last_notified_revision"], demand["revision"])
        self.assertEqual(demand["last_digest_id"], notification["digest_id"])
        self.assertEqual(demand["report_count"], 1)
        self.assertEqual(demand["next_notify_at"], "2026-11-02T14:00:00-03:00")
        self.assertEqual(self.cursor()["last_digest_at"], NOW)

    # ---- 3. 12 exibidos / 8 ocultos -----------------------------------
    def test_twenty_candidates_display_twelve_and_hide_eight(self) -> None:
        ids = self.add_many(20)
        log: list[str] = []
        code, _ = self.digest(sender=self.sent_sender(log))
        self.assertEqual(code, 0)
        self.assertIn("8 itens não exibidos", log[-1])

        notification = self.jsonl("notifications.jsonl")[-1]
        self.assertEqual(len(notification["items"]), 20)
        self.assertEqual(notification["displayed"], 12)
        self.assertEqual(notification["hidden"], 8)

        displayed = [item["demand_id"] for item in notification["items"][:12]]
        hidden = [item["demand_id"] for item in notification["items"][12:]]
        self.assertEqual(displayed, sorted(ids)[:12])
        self.assertEqual(hidden, sorted(ids)[12:])
        for demand_id in displayed:
            self.assertEqual(self.demand(demand_id)["reported_at"], NOW)
        for demand_id in hidden:
            demand = self.demand(demand_id)
            self.assertIsNone(demand["reported_at"])
            self.assertEqual(demand["last_notified_revision"], 0)
            self.assertEqual(demand["report_count"], 0)

    # ---- 4. briefing ---------------------------------------------------
    def test_briefing_sends_at_nine_and_not_at_seven(self) -> None:
        code, _ = self.digest(now="2026-11-01T07:00:00-03:00", sender=self.sent_sender())
        self.assertEqual(code, 0)
        self.assertEqual(self.jsonl("notifications.jsonl"), [])
        self.assertEqual(self.jsonl("digests.jsonl"), [])

        code, _ = self.digest(now="2026-11-01T09:00:00-03:00", sender=self.sent_sender())
        self.assertEqual(code, 0)
        notification = self.jsonl("notifications.jsonl")[-1]
        self.assertEqual(notification["kind"], "briefing")
        self.assertEqual(notification["items"], [])
        self.assertEqual(notification["status"], "sent")
        self.assertEqual(self.cursor()["last_digest_at"], "2026-11-01T09:00:00-03:00")

    # ---- 5. rollover ---------------------------------------------------
    def test_rollover_keeps_cursor_until_twelve_hours(self) -> None:
        self.add(
            "--importance", "1", "--urgency", "1", now="2026-11-01T06:00:00-03:00"
        )
        code, message = self.digest(
            now="2026-11-01T07:00:00-03:00", sender=self.sent_sender()
        )
        self.assertEqual(code, 0)
        self.assertEqual(message, "")
        notification = self.jsonl("notifications.jsonl")[-1]
        self.assertEqual(notification["status"], "rollover")
        self.assertEqual(notification["displayed"], 1)
        self.assertIsNone(self.cursor()["last_digest_at"])
        self.assertEqual(self.demand(self.jsonl("demands.jsonl")[0]["id"])["rollover"]["digest_count"], 1)

        code, message = self.digest(
            now="2026-11-02T03:00:00-03:00", sender=self.sent_sender()
        )
        self.assertEqual(code, 0)
        notification = self.jsonl("notifications.jsonl")[-1]
        self.assertEqual(notification["status"], "sent")
        self.assertEqual(self.jsonl("digests.jsonl")[-1]["reason"], "age")
        self.assertEqual(self.cursor()["last_digest_at"], "2026-11-02T03:00:00-03:00")

    # ---- 6. delivery_unknown ------------------------------------------
    def test_delivery_unknown_needs_force_to_resend(self) -> None:
        self.add("--importance", "5", "--urgency", "4")
        code, message = self.digest(sender=lambda message, payload: "unknown")
        self.assertEqual(code, 1)
        self.assertIn("entrega desconhecida", message)
        unknown = self.jsonl("notifications.jsonl")[-1]
        self.assertEqual(unknown["status"], "delivery_unknown")

        code, _ = outbox.run(Store(now=LATER), retry=True, sender=self.sent_sender())
        self.assertEqual(code, 0)
        untouched = self.jsonl("notifications.jsonl")[-1]
        self.assertEqual(untouched["status"], "delivery_unknown")
        self.assertIsNone(untouched["message_id"])
        self.assertEqual(untouched["attempts"], 1)

        code, message = outbox.run(
            Store(now=LATER), retry=True, force=True,
            sender=self.sent_sender(message_id="msg-9"),
        )
        self.assertEqual(code, 0, message)
        retried = self.jsonl("notifications.jsonl")[-1]
        self.assertEqual(retried["status"], "sent")
        self.assertEqual(retried["message_id"], "msg-9")

    # ---- 7. rebuild ----------------------------------------------------
    def test_rebuild_replays_notifications_and_keeps_cursor(self) -> None:
        self.add("--importance", "5", "--urgency", "4")
        self.digest(sender=self.sent_sender())
        before_notifications = (self.home / "state" / "notifications.jsonl").read_bytes()
        before_cursor = (self.home / "state" / "cursor.json").read_bytes()
        before_demands = self.jsonl("demands.jsonl")

        Store(now=NOW).rebuild()

        self.assertEqual(
            (self.home / "state" / "notifications.jsonl").read_bytes(),
            before_notifications,
        )
        self.assertEqual((self.home / "state" / "cursor.json").read_bytes(), before_cursor)
        self.assertEqual(self.jsonl("demands.jsonl"), before_demands)

    # ---- 8. kill entre sender e update --------------------------------
    def test_crash_between_send_and_update_marks_delivery_unknown(self) -> None:
        self.add("--importance", "5", "--urgency", "4")

        def dying(message: str, payload: dict) -> str:
            raise SystemExit(99)

        with self.assertRaises(SystemExit):
            self.digest(sender=dying)

        pending = self.jsonl("notifications.jsonl")[-1]
        self.assertEqual(pending["status"], "pending")
        self.assertEqual(pending["attempts"], 1)
        self.assertIsNone(self.cursor()["last_digest_at"])

        outbox.run(Store(now=LATER), retry=False)
        resolved = self.jsonl("notifications.jsonl")[-1]
        self.assertEqual(resolved["status"], "delivery_unknown")
        self.assertIn("morreu", resolved["error"])


if __name__ == "__main__":
    unittest.main()
