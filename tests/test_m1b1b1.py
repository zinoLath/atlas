"""Testes do M1b.1b1: `ingest.add_trigger` reutilizável pelo reader."""

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

from atlas_cli import ingest  # noqa: E402
from atlas_cli.ingest import add_trigger  # noqa: E402
from atlas_cli.store import Store  # noqa: E402
from atlas_cli.util import AtlasError  # noqa: E402


class M1b1b1TestCase(unittest.TestCase):
    def setUp(self) -> None:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.home = Path(tempfile.mkdtemp(prefix="atlas-m1b1b1-", dir=TMP_ROOT))
        self.store = Store(home=self.home, now=NOW)
        self.store.init()

    def tearDown(self) -> None:
        shutil.rmtree(self.home, ignore_errors=True)

    # ---- helpers ------------------------------------------------------
    def spec(self, **overrides) -> dict:
        spec = {
            "source": "email",
            "external_id": "email:eu:rfc:a@b",
            "title": "Assunto",
            "body": "corpo normalizado",
            "actor": "alice@example.com",
        }
        spec.update(overrides)
        return spec

    def jsonl(self, name: str) -> list[dict]:
        path = self.home / "state" / name
        if not path.exists():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def triggers(self) -> list[dict]:
        return self.jsonl("triggers.jsonl")

    def threads(self) -> list[dict]:
        return self.jsonl("threads.jsonl")

    def blob_path(self, body: str) -> Path:
        sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
        return self.home / "raw" / "blobs" / sha[:2] / f"{sha}.txt"

    def run_atlas(self, *args: str) -> subprocess.CompletedProcess:
        env = dict(os.environ, ATLAS_HOME=str(self.home))
        return subprocess.run(
            [sys.executable, str(BIN), "--now", NOW, *args],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )

    # ---- 1. criação direta -------------------------------------------
    def test_add_trigger_cria_trigger_thread_e_blob(self) -> None:
        trigger = add_trigger(self.store, self.spec())
        self.assertIsInstance(trigger, dict)
        self.assertEqual(trigger["state"], "new")
        self.assertEqual(trigger["kind"], "email")
        self.assertIs(trigger["untrusted"], True)
        self.assertEqual(trigger["actor"], "alice@example.com")
        self.assertEqual(trigger["thread_key"], "email:email:eu:rfc:a@b")
        self.assertNotIn(ingest.REPAIRED_KEY, trigger)

        rows = self.triggers()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], trigger["id"])
        self.assertEqual(rows[0]["received_at"], NOW)
        self.assertEqual(
            rows[0]["body_path"], f"raw/blobs/{trigger['body_sha256'][:2]}/"
            f"{trigger['body_sha256']}.txt",
        )

        thread = self.threads()[0]
        self.assertEqual(thread["id"], trigger["thread_id"])
        self.assertEqual(thread["external_id"], "email:email:eu:rfc:a@b")
        self.assertEqual(thread["last_trigger_at"], NOW)

        blob = self.blob_path("corpo normalizado")
        self.assertTrue(blob.exists())
        self.assertEqual(blob.read_text(encoding="utf-8"), "corpo normalizado")

    def test_trusted_so_para_manual(self) -> None:
        trigger = add_trigger(
            self.store,
            self.spec(source="manual", external_id="manual-1", trusted=True),
        )
        self.assertIs(trigger["untrusted"], False)
        with self.assertRaises(AtlasError):
            add_trigger(
                self.store, self.spec(external_id="email-2", trusted=True)
            )

    # ---- 2. meta e labels --------------------------------------------
    def test_meta_shadow_account_e_labels_preservados(self) -> None:
        meta = {
            "shadow": True,
            "account": "eu@example.com",
            "mailbox": "INBOX",
            "uidvalidity": 123456789,
            "uid": 4821,
            "message_key": "email:eu:rfc:a@b",
            "occurrences": [{"mailbox": "INBOX", "uid": 4821}],
            "headers": {"subject": "Assunto bruto"},
            "labels": ["INBOX"],
        }
        trigger = add_trigger(self.store, self.spec(meta=meta))
        meta["account"] = "mutado"; meta["headers"]["subject"] = "mutado"

        stored = self.triggers()[0]
        self.assertEqual(stored["id"], trigger["id"])
        self.assertIs(stored["meta"]["shadow"], True)
        self.assertEqual(stored["meta"]["account"], "eu@example.com")
        self.assertEqual(stored["meta"]["message_key"], "email:eu:rfc:a@b")
        self.assertEqual(stored["meta"]["occurrences"], [{"mailbox": "INBOX", "uid": 4821}])
        self.assertEqual(stored["meta"]["headers"], {"subject": "Assunto bruto"})
        self.assertEqual(stored["meta"]["labels"], ["INBOX"])

    def test_labels_do_spec_prevalecem_sobre_meta(self) -> None:
        trigger = add_trigger(
            self.store, self.spec(meta={"labels": ["INBOX"]}, labels=["INBOX", "Arquivo"])
        )
        self.assertEqual(self.trigger(trigger["id"])["meta"]["labels"], ["INBOX", "Arquivo"])

    def test_meta_nao_sobrescreve_identidade(self) -> None:
        for key in ("external_id", "thread_id", "body_path", "body_sha256", "state"):
            with self.subTest(key=key):
                with self.assertRaises(AtlasError) as ctx:
                    add_trigger(self.store, self.spec(meta={key: "x"}))
                self.assertIn(key, str(ctx.exception))
        self.assertEqual(self.triggers(), [])
        self.assertFalse((self.home / "raw" / "blobs").exists())

    def test_meta_thread_key_divergente_falha_mas_igual_e_aceito(self) -> None:
        with self.assertRaises(AtlasError):
            add_trigger(
                self.store,
                self.spec(thread_key="email:t1", meta={"thread_key": "email:t2"}),
            )
        trigger = add_trigger(
            self.store,
            self.spec(thread_key="email:t1", meta={"thread_key": "email:t1"}),
        )
        self.assertEqual(trigger["thread_key"], "email:t1")

    # ---- 3. idempotência e reparo ------------------------------------
    def test_mesmo_corpo_e_idempotente(self) -> None:
        first = add_trigger(self.store, self.spec())
        second = add_trigger(self.store, self.spec())
        self.assertEqual(first["id"], second["id"])
        self.assertNotIn(ingest.REPAIRED_KEY, second)
        self.assertEqual(len(self.triggers()), 1)
        self.assertEqual(len(self.threads()), 1)
        self.assertEqual(len(list((self.home / "raw" / "blobs").glob("*/*.txt"))), 1)

    def test_corpo_diferente_falha(self) -> None:
        add_trigger(self.store, self.spec(body="corpo A"))
        with self.assertRaises(AtlasError) as ctx:
            add_trigger(self.store, self.spec(body="corpo B"))
        self.assertIn("corpo diferente", str(ctx.exception))
        self.assertEqual(len(self.triggers()), 1)
        self.assertEqual(
            self.triggers()[0]["body_sha256"], hashlib.sha256(b"corpo A").hexdigest()
        )

    def test_blob_apagado_repara(self) -> None:
        first = add_trigger(self.store, self.spec())
        blob = self.blob_path("corpo normalizado")
        blob.unlink()

        repaired = add_trigger(self.store, self.spec())
        self.assertEqual(repaired["id"], first["id"])
        self.assertIs(repaired.get(ingest.REPAIRED_KEY), True)
        self.assertTrue(blob.exists())
        self.assertEqual(blob.read_text(encoding="utf-8"), "corpo normalizado")
        self.assertEqual(len(self.triggers()), 1)
        self.assertEqual(self.trigger(first["id"])["body_path"], first["body_path"])

    def test_thread_reusa_e_last_trigger_at_e_maximo(self) -> None:
        add_trigger(
            self.store,
            self.spec(
                external_id="email:1",
                thread_key="thread-x",
                received_at="2026-11-01T10:00:00-03:00",
            ),
        )
        newer = add_trigger(
            self.store,
            self.spec(
                external_id="email:2",
                thread_key="thread-x",
                received_at="2026-11-01T12:00:00-03:00",
            ),
        )
        older = add_trigger(
            self.store,
            self.spec(
                external_id="email:3",
                thread_key="thread-x",
                received_at="2026-11-01T09:00:00-03:00",
            ),
        )
        self.assertEqual(newer["thread_id"], older["thread_id"])
        thread = self.threads()[0]
        self.assertEqual(thread["last_trigger_at"], "2026-11-01T12:00:00-03:00")
        self.assertEqual(len(self.threads()), 1)

    def trigger(self, trigger_id: str) -> dict:
        return {row["id"]: row for row in self.triggers()}[trigger_id]

    # ---- 4. CLI inalterado -------------------------------------------
    def test_cli_trigger_add_mantem_saida(self) -> None:
        args = (
            "trigger", "add", "--source", "email", "--external-id", "cli-1",
            "--title", "Evento", "--body", "corpo cli", "--label", "INBOX",
        )
        first = self.run_atlas(*args)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertRegex(
            first.stdout.strip(),
            r"^trg-\d{8}-\d{4} thread=thr-\d{8}-\d{4} state=new$",
        )
        second = self.run_atlas(*args)
        self.assertEqual(second.stdout, first.stdout)

        blob = self.blob_path("corpo cli")
        blob.unlink()
        third = self.run_atlas(*args)
        self.assertEqual(third.stdout, first.stdout.strip() + " reparado\n")

        stored = self.triggers()
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["meta"], {"labels": ["INBOX"]})


if __name__ == "__main__":
    unittest.main()
