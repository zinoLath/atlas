"""Testes do M1b.0b: context pack (Inline & Spill) somente leitura."""

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


class M1b0bTestCase(unittest.TestCase):
    def setUp(self) -> None:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.home = Path(tempfile.mkdtemp(prefix="atlas-m1b0b-", dir=TMP_ROOT))
        self.env = dict(os.environ, ATLAS_HOME=str(self.home))
        self.ok("init")
        self.ok(
            "kb",
            "add",
            "--parent",
            "",
            "--kind",
            "area",
            "--title",
            "projetos",
            "--summary",
            "Área de projetos.",
        )
        self.node = self.ok(
            "kb",
            "add",
            "--parent",
            "projetos",
            "--kind",
            "artigo",
            "--title",
            "Nota de teste",
            "--summary",
            "Resumo de teste sobre propostas.",
            "--body-file",
            self.write_file("kb-body.md", "# Nota de teste\n\nCorpo do nó.\n"),
        ).strip()
        self.demand = self.add_demand(kb=self.node)
        out = self.add_trigger("msg-1", body="Corpo bruto do evento.")
        self.trigger = out.split()[0]
        self.thread = self.thread_of(out)
        self.ok("subscribe", self.demand, self.thread)

    def tearDown(self) -> None:
        shutil.rmtree(self.home, ignore_errors=True)

    # ---- helpers ------------------------------------------------------
    def run_atlas(self, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(BIN), "--now", NOW, *args],
            capture_output=True,
            text=True,
            env=env or self.env,
            check=False,
        )

    def ok(self, *args: str) -> str:
        proc = self.run_atlas(*args)
        self.assertEqual(proc.returncode, 0, f"stdout={proc.stdout}\nstderr={proc.stderr}")
        return proc.stdout

    def write_file(self, name: str, text: str) -> str:
        path = self.home / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return str(path)

    def add_demand(self, **kwargs) -> str:
        args = ["add", "--area", "projetos", "--title", "Demanda teste"]
        for key, value in kwargs.items():
            if key == "kb":
                args.extend(["--kb", value])
        out = self.ok(*args)
        return out.split()[0]

    def add_trigger(self, external_id: str, *, body: str | None = None) -> str:
        args = [
            "trigger",
            "add",
            "--source",
            "email",
            "--external-id",
            external_id,
            "--title",
            "Evento teste",
            "--actor",
            "colega@example.com",
        ]
        if body is not None:
            args.extend(["--body", body])
        return self.ok(*args)

    def thread_of(self, stdout: str) -> str:
        return stdout.split("thread=", 1)[1].split()[0]

    def context(self, *extra: str) -> str:
        return self.ok("context", self.demand, *extra)

    def pack(self, *extra: str) -> dict:
        return json.loads(self.context("--json", *extra))

    # ---- 1. pacote completo -------------------------------------------
    def test_pack_has_demand_thread_trigger_and_kb_pointer(self) -> None:
        pack = self.pack()
        self.assertEqual(pack["demand"]["id"], self.demand)
        self.assertEqual(pack["demand"]["thread_ids"], [self.thread])
        self.assertEqual(len(pack["threads"]), 1)
        thread = pack["threads"][0]
        self.assertEqual(thread["id"], self.thread)
        self.assertEqual(thread["triggers"][0]["id"], self.trigger)
        trigger = self.trigger_of(self.trigger)
        self.assertEqual(thread["triggers"][0]["body_path"], trigger["body_path"])
        self.assertTrue(any(item["id"] == self.node for item in pack["kb"]))
        self.assertTrue(
            any(item["path"] == trigger["body_path"] for item in pack["spill"])
        )
        self.assertTrue(
            any(item["path"] == f"kb/{self.node}.md" for item in pack["spill"])
        )
        self.assertTrue(
            any(item["path"] == f"demands/{self.demand}.md" for item in pack["spill"])
        )

    def trigger_of(self, trigger_id: str) -> dict:
        path = self.home / "state" / "triggers.jsonl"
        rows = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        return {row["id"]: row for row in rows}[trigger_id]

    # ---- 2. orçamento --------------------------------------------------
    def test_small_budget_truncates_but_keeps_header_and_spill(self) -> None:
        out = self.context("--budget", "400")
        self.assertIn(f"# CONTEXT {self.demand}", out)
        self.assertIn("## spill", out)
        self.assertIn("truncado", out)
        pack = self.pack("--budget", "400")
        self.assertTrue(pack["budget"]["truncated"])
        self.assertTrue(pack["budget"]["omitted"])
        self.assertEqual(pack["budget"]["limit"], 400)
        self.assertLessEqual(len(pack["kb"]), 3)

    # ---- 3. expand de trigger ------------------------------------------
    def test_expand_trigger_prints_raw_body(self) -> None:
        out = self.context("--expand", self.trigger)
        self.assertIn(self.trigger, out)
        self.assertIn("Corpo bruto do evento.", out)
        out = self.context("--expand", self.thread)
        self.assertIn(self.trigger, out)
        self.assertIn("Evento teste", out)

    # ---- 4. expand de demanda ------------------------------------------
    def test_expand_demand_prints_json_and_markdown(self) -> None:
        out = self.context("--expand", self.demand)
        self.assertIn(f'"id": "{self.demand}"', out)
        self.assertIn('"title": "Demanda teste"', out)
        self.assertIn(f"demands/{self.demand}.md", out)

    # ---- 5. expand desconhecido ----------------------------------------
    def test_expand_unknown_id_exits_2(self) -> None:
        for object_id in ("trg-20990101-0001", "thr-20990101-0001", "dmd-20990101-0001", "sem/no"):
            proc = self.run_atlas("context", self.demand, "--expand", object_id)
            self.assertEqual(proc.returncode, 2, proc.stderr)
            self.assertIn("inexistente", proc.stderr)

    # ---- 6. sem segredos ------------------------------------------------
    def test_pack_and_expand_never_leak_secrets(self) -> None:
        env = dict(self.env, ATLAS_IMAP_PASS="hunter2")
        secrets = self.write_file(
            "secrets.env",
            "ATLAS_IMAP_PASS=hunter2\nSMTP_TOKEN=s3cr3t\n",
        )
        self.assertIn("hunter2", Path(secrets).read_text(encoding="utf-8"))
        leaky = self.add_trigger(
            "msg-2", body="API_KEY=topsecret\nresto ok\n"
        ).split()[0]
        for command in (
            ("context", self.demand),
            ("context", self.demand, "--json"),
            ("context", self.demand, "--expand", leaky),
        ):
            proc = self.run_atlas(*command, env=env)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            for needle in ("hunter2", "s3cr3t", "topsecret"):
                self.assertNotIn(needle, proc.stdout)
        out = self.run_atlas("context", self.demand, "--expand", leaky, env=env).stdout
        self.assertIn("***", out)

    # ---- 7. determinismo ------------------------------------------------
    def test_pack_is_deterministic(self) -> None:
        first = self.context("--json")
        second = self.context("--json")
        self.assertEqual(first, second)
        self.assertEqual(self.context(), self.context())


if __name__ == "__main__":
    unittest.main()
