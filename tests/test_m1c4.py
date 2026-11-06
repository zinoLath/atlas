"""Testes do M1c.4: backup inclui estado não reconstruível e restaura."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BIN = REPO / "bin" / "atlas"
TMP_ROOT = REPO / "tests" / ".tmp"
NOW = "2026-11-01T14:00:00-03:00"
MARKER = "linha marcadora do corpo da demanda"
BLOB_BODY = "corpo bruto do trigger de backup\n"
RULES = {
    "version": 1,
    "default": "candidate",
    "rules": [
        {
            "name": "backup-financas",
            "match": {"source": "email", "subject_regex": "(?i)backup-rules"},
            "action": "route",
            "area": "financas",
        }
    ],
}

sys.path.insert(0, str(REPO))

from atlas_cli import backup, ingest, render, triggers  # noqa: E402
from atlas_cli.store import Store  # noqa: E402


class BackupRestoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.home = Path(tempfile.mkdtemp(prefix="atlas-m1c4-", dir=TMP_ROOT))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.store = Store(home=self.home, now=NOW)
        self.store.init()

    def run_atlas(self, *args: str) -> subprocess.CompletedProcess:
        env = dict(os.environ, ATLAS_HOME=str(self.home))
        return subprocess.run(
            [sys.executable, str(BIN), "--now", NOW, *args],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )

    def seed(self) -> tuple[str, dict]:
        body_file = self.home / "body.md"
        body_file.write_text(f"# Demanda\n\n{MARKER}\n", encoding="utf-8")
        proc = self.run_atlas(
            "add",
            "--area",
            "projetos",
            "--title",
            "Demanda backupável",
            "--body-file",
            str(body_file),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        demand = self.store.demands()[0]

        rules_path = self.store.rules_path
        rules_path.write_text(
            json.dumps(RULES, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

        trigger = ingest.add_trigger(
            self.store,
            {
                "source": "email",
                "external_id": "<backup@example.com>",
                "title": "Assunto backup-rules",
                "body": BLOB_BODY,
            },
        )
        return demand["id"], trigger

    def test_restore_completo_em_home_novo(self) -> None:
        demand_id, trigger = self.seed()
        self.assertEqual(len(self.store.triggers()), 1)
        self.assertTrue(self.store.db_path.is_file())
        (self.home / "secrets.env").write_text("ATLAS_IMAP_PASS=segredo\n", encoding="utf-8")
        (self.home / "tests").mkdir(parents=True, exist_ok=True)
        (self.home / "tests" / "test_x.py").write_text("x = 1\n", encoding="utf-8")

        result = backup.create(self.store, now=NOW)

        with tarfile.open(result["path"], "r:gz") as archive:
            names = archive.getnames()
        self.assertIn(f"demands/{demand_id}.md", names)
        self.assertIn("router/rules.v1.json", names)
        self.assertIn("areas/projetos/CHARTER.md", names)
        self.assertIn("state/demands.jsonl", names)
        self.assertIn("state/transactions.jsonl", names)
        self.assertIn(trigger["body_path"], names)
        for excluded in ("secrets.env", "state/.lock", "state/hub.db"):
            self.assertNotIn(excluded, names)
        self.assertFalse(any(name.startswith("tests/") for name in names))

        target = Path(tempfile.mkdtemp(prefix="atlas-m1c4-restore-", dir=TMP_ROOT))
        self.addCleanup(shutil.rmtree, target, ignore_errors=True)
        with tarfile.open(result["path"], "r:gz") as archive:
            archive.extractall(target)

        restored = Store(home=target, now=NOW)
        restored.init()
        restored.rebuild()

        # Demandas: metadados e corpo preservados.
        self.assertEqual(restored.demands(), self.store.demands())
        original_body = render.read_body(self.store.demand_md(demand_id))
        restored_body = render.read_body(restored.demand_md(demand_id))
        self.assertEqual(restored_body, original_body)
        self.assertIn(MARKER, restored_body)

        # Regras de rota e decisão do fast path.
        rules_before = triggers.load_rules(self.home)
        rules_after = triggers.load_rules(target)
        self.assertEqual(
            (rules_after["default"], [rule["name"] for rule in rules_after["rules"]]),
            (rules_before["default"], [rule["name"] for rule in rules_before["rules"]]),
        )
        decision_before = triggers.decide(trigger, [], rules_before)
        trigger_after = restored.triggers()[0]
        self.assertEqual(trigger_after, trigger)
        decision_after = triggers.decide(trigger_after, [], rules_after)
        self.assertEqual(decision_after, decision_before)
        self.assertEqual(decision_after["state"], "routed")
        self.assertEqual(decision_after["route"]["area"], "financas")

        # Blob do trigger.
        blob = target / trigger_after["body_path"]
        self.assertEqual(blob.read_text(encoding="utf-8"), BLOB_BODY)


if __name__ == "__main__":
    unittest.main()
