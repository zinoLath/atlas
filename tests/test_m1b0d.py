"""Testes do M1b.0d: BLOB-01, WAL-01, DROP-01 e ACK-01."""

from __future__ import annotations

import hashlib
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

sys.path.insert(0, str(REPO))

from atlas_cli import digest, notifications  # noqa: E402
from atlas_cli.store import Store  # noqa: E402


class M1b0dTestCase(unittest.TestCase):
    def setUp(self) -> None:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.home = Path(tempfile.mkdtemp(prefix="atlas-m1b0d-", dir=TMP_ROOT))
        self.env = dict(os.environ, ATLAS_HOME=str(self.home))
        self.ok("init")

    def tearDown(self) -> None:
        shutil.rmtree(self.home, ignore_errors=True)

    # ---- helpers ------------------------------------------------------
    def run_atlas(
        self, *args: str, env_extra: dict | None = None
    ) -> subprocess.CompletedProcess:
        env = dict(self.env)
        if env_extra:
            env.update(env_extra)
        return subprocess.run(
            [sys.executable, str(BIN), "--now", NOW, *args],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )

    def ok(self, *args: str, env_extra: dict | None = None) -> str:
        proc = self.run_atlas(*args, env_extra=env_extra)
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

    def add_demand(self, title: str = "Demanda teste") -> str:
        return self.ok("add", "--area", "projetos", "--title", title).split()[0]

    def add_trigger(self, external_id: str, body: str, title: str = "Evento teste") -> str:
        return self.ok(
            "trigger",
            "add",
            "--source",
            "email",
            "--external-id",
            external_id,
            "--title",
            title,
            "--body",
            body,
        )

    def trigger(self, trigger_id: str) -> dict:
        return {row["id"]: row for row in self.jsonl("triggers.jsonl")}[trigger_id]

    def blob_path(self, body: str) -> Path:
        sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
        return self.home / "raw" / "blobs" / sha[:2] / f"{sha}.txt"

    # ---- 1. BLOB-01 ---------------------------------------------------
    def test_add_writes_content_addressed_blob(self) -> None:
        body = "primeira linha\nsegunda linha\n"
        trigger_id = self.add_trigger("blob-1", body).split()[0]
        trigger = self.trigger(trigger_id)
        blob = self.blob_path(body)
        self.assertEqual(trigger["body_path"], str(blob.relative_to(self.home)))
        self.assertEqual(trigger["body_sha256"], hashlib.sha256(body.encode()).hexdigest())
        self.assertEqual(blob.read_text(encoding="utf-8"), body)
        self.assertFalse((self.home / "raw" / "triggers").exists())
        self.assertEqual(self.ok("doctor").strip(), "doctor ok")

    def test_add_same_body_is_idempotent(self) -> None:
        first = self.add_trigger("blob-2", "corpo igual")
        second = self.add_trigger("blob-2", "corpo igual")
        self.assertEqual(first, second)
        self.assertEqual(len(self.jsonl("triggers.jsonl")), 1)
        blobs = list((self.home / "raw" / "blobs").glob("*/*.txt"))
        self.assertEqual(len(blobs), 1)

    def test_add_different_body_for_same_external_id_fails(self) -> None:
        self.add_trigger("blob-3", "corpo A")
        proc = self.run_atlas(
            "trigger",
            "add",
            "--source",
            "email",
            "--external-id",
            "blob-3",
            "--title",
            "Evento teste",
            "--body",
            "corpo B",
        )
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("corpo diferente", proc.stderr)
        self.assertEqual(len(self.jsonl("triggers.jsonl")), 1)
        self.assertEqual(self.trigger(self.jsonl("triggers.jsonl")[0]["id"])["body_sha256"],
                         hashlib.sha256(b"corpo A").hexdigest())

    def test_missing_blob_is_repaired(self) -> None:
        body = "corpo reparavel"
        trigger_id = self.add_trigger("blob-4", body).split()[0]
        blob = self.blob_path(body)
        blob.unlink()
        out = self.add_trigger("blob-4", body)
        self.assertIn("reparado", out)
        self.assertTrue(blob.exists())
        self.assertEqual(blob.read_text(encoding="utf-8"), body)
        self.assertEqual(len(self.jsonl("triggers.jsonl")), 1)
        self.assertEqual(self.trigger(trigger_id)["body_path"],
                         str(blob.relative_to(self.home)))

    def test_doctor_flags_missing_blob(self) -> None:
        body = "corpo ausente"
        self.add_trigger("blob-5", body)
        self.blob_path(body).unlink()
        proc = self.run_atlas("doctor")
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("trigger sem corpo", proc.stdout)

    def test_doctor_flags_tampered_blob(self) -> None:
        body = "corpo original"
        self.add_trigger("blob-6", body)
        self.blob_path(body).write_text("adulterado", encoding="utf-8")
        proc = self.run_atlas("doctor")
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("corpo adulterado", proc.stdout)

    def test_crash_after_blob_leaves_orphan_without_trigger(self) -> None:
        body = "corpo orfao"
        proc = self.run_atlas(
            "trigger",
            "add",
            "--source",
            "email",
            "--external-id",
            "blob-7",
            "--title",
            "Evento teste",
            "--body",
            body,
            env_extra={"ATLAS_TEST_CRASH_AFTER_TRIGGER_BLOB": "1"},
        )
        self.assertEqual(proc.returncode, 98, proc.stderr)
        self.assertEqual(self.jsonl("triggers.jsonl"), [])
        blob = self.blob_path(body)
        self.assertTrue(blob.exists())
        # O próximo add funciona e reaproveita o blob órfão.
        self.add_trigger("blob-7", body)
        self.assertEqual(len(self.jsonl("triggers.jsonl")), 1)
        self.assertEqual(blob.read_text(encoding="utf-8"), body)

    # ---- 2. WAL-01 ----------------------------------------------------
    def transactions_bytes(self) -> bytes:
        return (self.home / "state" / "transactions.jsonl").read_bytes()

    def test_partial_tail_is_truncated_before_append(self) -> None:
        self.add_demand("primeira")
        before = self.jsonl("transactions.jsonl")
        self.assertEqual(len(before), 1)
        path = self.home / "state" / "transactions.jsonl"
        with path.open("ab") as fh:
            fh.write(b'{"bad":')

        self.add_demand("segunda")

        lines = [
            line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
        ]
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0])["tx_id"], before[0]["tx_id"])
        for line in lines:
            self.assertIsInstance(json.loads(line), dict)
        self.assertEqual(self.ok("doctor").strip(), "doctor ok")
        self.ok("rebuild")
        self.assertEqual(len(self.jsonl("demands.jsonl")), 2)

    def test_complete_tail_without_newline_is_preserved(self) -> None:
        self.add_demand("primeira")
        path = self.home / "state" / "transactions.jsonl"
        data = path.read_bytes()
        self.assertTrue(data.endswith(b"\n"))
        path.write_bytes(data[:-1])
        before = [json.loads(line) for line in data.splitlines() if line.strip()]
        self.assertEqual(len(before), 1)

        self.add_demand("segunda")

        lines = [
            line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
        ]
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0])["tx_id"], before[0]["tx_id"])
        self.assertTrue(path.read_bytes().endswith(b"\n"))

    def test_partial_utf8_tail_is_truncated(self) -> None:
        self.add_demand("primeira")
        path = self.home / "state" / "transactions.jsonl"
        with path.open("ab") as fh:
            fh.write(b"\xc3")

        self.add_demand("segunda")

        data = path.read_bytes()
        self.assertTrue(data.endswith(b"\n"))
        lines = [line for line in data.decode("utf-8").splitlines() if line.strip()]
        self.assertEqual(len(lines), 2)
        for line in lines:
            self.assertIsInstance(json.loads(line), dict)
        self.assertEqual(len(self.jsonl("demands.jsonl")), 2)

    # ---- 4. ACK-01 ----------------------------------------------------
    def test_mark_reported_uses_frozen_revision(self) -> None:
        demand_id = self.add_demand("demanda revisada")
        notification = {
            "id": "ntf-test",
            "digest_id": "dig-test",
            "displayed": 1,
            "items": [{"demand_id": demand_id, "revision": 1, "score": 5}],
        }
        # Revisão 2 depois de a notificação ter congelado a revisão 1.
        self.ok("classify", demand_id)
        store = Store(home=self.home, now=NOW)
        demand = next(row for row in store.demands() if row["id"] == demand_id)
        self.assertEqual(demand["revision"], 2)

        with store.transaction("test.ack") as tx:
            notifications.mark_reported(tx, notification, store.now_iso, store.dt)

        demand = next(row for row in store.demands() if row["id"] == demand_id)
        self.assertEqual(demand["last_notified_revision"], 1)
        self.assertTrue(digest.is_candidate(demand, store.dt))


if __name__ == "__main__":
    unittest.main()
