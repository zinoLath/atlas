"""Testes do M1b.0f-D2: BLOB-01, BACKUP-02 e STATE-02."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BIN = REPO / "bin" / "atlas"
TMP_ROOT = REPO / "tests" / ".tmp"
NOW = "2026-11-01T14:00:00-03:00"
MARKER = "corpo que o add concorrente nao pode perder"
ACCOUNT = "usuario"
MAILBOX = "INBOX"

sys.path.insert(0, str(REPO))

from atlas_cli import backup, control, imap_reader, ingest, render, triggers  # noqa: E402
from atlas_cli.store import Store  # noqa: E402
from atlas_cli.util import sha256_text, write_text  # noqa: E402


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.home = Path(tempfile.mkdtemp(prefix="atlas-m1b0f-d-", dir=TMP_ROOT))
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

    def start_atlas(self, *args: str) -> subprocess.Popen:
        env = dict(os.environ, ATLAS_HOME=str(self.home))
        return subprocess.Popen(
            [sys.executable, str(BIN), "--now", NOW, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )

    def extract(self, tar_path: str | Path) -> Path:
        target = Path(tempfile.mkdtemp(prefix="atlas-m1b0f-d-restore-", dir=TMP_ROOT))
        self.addCleanup(shutil.rmtree, target, ignore_errors=True)
        with tarfile.open(tar_path, "r:gz") as archive:
            archive.extractall(target)
        return target


# ---------------------------------------------------------------------------
# BLOB-01
# ---------------------------------------------------------------------------
class BlobConcurrencyTestCase(_Base):
    def test_publicacao_concorrente_no_mesmo_blob(self) -> None:
        """Dois escritores no mesmo alvo publicam bytes completos, nunca mistos."""
        target = self.home / "raw" / "blobs" / "aa" / ("aa" + "0" * 62 + ".txt")
        payloads = [
            (f"escritor-{index}\n" + chr(65 + index) * 200_000 + f"\nfim-{index}\n")
            for index in range(8)
        ]
        barrier = threading.Barrier(len(payloads))
        failures: list[BaseException] = []

        def run(payload: str) -> None:
            try:
                barrier.wait()
                for _ in range(4):
                    write_text(target, payload)
            except BaseException as exc:  # pragma: no cover - falha explícita
                failures.append(exc)

        threads = [threading.Thread(target=run, args=(payload,)) for payload in payloads]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(failures, [])
        final = target.read_text(encoding="utf-8")
        self.assertIn(final, payloads)
        self.assertEqual(list(target.parent.glob("*.tmp")), [])

    def test_write_blob_concorrente_mantem_hash(self) -> None:
        body = "corpo do trigger\n" * 20_000
        body_sha = sha256_text(body)
        body_rel = triggers.blob_relpath(body_sha)
        barrier = threading.Barrier(2)
        failures: list[BaseException] = []

        def run() -> None:
            try:
                barrier.wait()
                ingest._write_blob(self.store, body, body_sha, body_rel)
            except BaseException as exc:  # pragma: no cover - falha explícita
                failures.append(exc)

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(failures, [])
        blob = self.home / body_rel
        self.assertEqual(sha256_text(blob.read_text(encoding="utf-8")), body_sha)


# ---------------------------------------------------------------------------
# BACKUP-02
# ---------------------------------------------------------------------------
class BackupCursorControlTestCase(_Base):
    def _run_writer(self, write) -> tuple[threading.Event, list[BaseException], threading.Thread]:
        stop = threading.Event()
        failures: list[BaseException] = []

        def loop() -> None:
            index = 0
            while not stop.is_set():
                try:
                    write(index)
                except BaseException as exc:  # pragma: no cover - falha explícita
                    failures.append(exc)
                    return
                index += 1
                time.sleep(0.001)

        thread = threading.Thread(target=loop)
        thread.start()
        time.sleep(0.02)
        return stop, failures, thread

    def test_cursor_e_control_esperam_o_lock_dos_escritores(self) -> None:
        cursor = imap_reader.empty_cursor(ACCOUNT, MAILBOX)
        cursor["last_uid"] = 7

        def write_cursor_thread() -> None:
            imap_reader.replace_cursor_snapshot(self.store, cursor)

        def write_control_thread() -> None:
            control.pause(self.store, reason="concorrente", now=NOW)

        with self.store.lock():
            threads = [
                threading.Thread(target=write_cursor_thread),
                threading.Thread(target=write_control_thread),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=0.2)
            self.assertTrue(all(thread.is_alive() for thread in threads))

        for thread in threads:
            thread.join(timeout=5)
        self.assertFalse([thread for thread in threads if thread.is_alive()])
        self.assertEqual(
            imap_reader.read_cursor(self.store, ACCOUNT, MAILBOX)["last_uid"], 7
        )
        self.assertTrue(control.read_control(self.store)["paused"])

    def test_backup_nao_captura_cursor_parcial(self) -> None:
        def write(index: int) -> None:
            cursor = imap_reader.empty_cursor(ACCOUNT, MAILBOX)
            cursor.update(
                {
                    "uidvalidity": 123,
                    "last_uid": index + 1,
                    "padding": "x" * 200_000,
                }
            )
            imap_reader.replace_cursor_snapshot(self.store, cursor)

        stop, failures, thread = self._run_writer(write)
        try:
            result = backup.create(self.store, now=NOW)
        finally:
            stop.set()
            thread.join()

        self.assertEqual(failures, [])
        cursor_name = f"state/cursors/{imap_reader.cursor_path(self.store, ACCOUNT, MAILBOX).name}"
        with tarfile.open(result["path"], "r:gz") as archive:
            names = archive.getnames()
        self.assertIn(cursor_name, names)
        self.assertFalse([name for name in names if name.endswith(".tmp")])

        target = self.extract(result["path"])
        cursor = imap_reader.read_cursor(Store(home=target, now=NOW), ACCOUNT, MAILBOX)
        self.assertEqual(cursor["account"], ACCOUNT)
        self.assertGreaterEqual(cursor["last_uid"], 1)

    def test_backup_nao_captura_control_parcial(self) -> None:
        def write(index: int) -> None:
            if index % 2:
                control.resume(self.store, now=NOW)
            else:
                control.pause(self.store, reason="y" * 200_000, now=NOW)

        stop, failures, thread = self._run_writer(write)
        try:
            result = backup.create(self.store, now=NOW)
        finally:
            stop.set()
            thread.join()

        self.assertEqual(failures, [])
        with tarfile.open(result["path"], "r:gz") as archive:
            names = archive.getnames()
        self.assertIn("state/control.json", names)
        self.assertFalse([name for name in names if name.endswith(".tmp")])

        target = self.extract(result["path"])
        restored = control.read_control(Store(home=target, now=NOW))
        self.assertIn("paused", restored)


# ---------------------------------------------------------------------------
# STATE-02
# ---------------------------------------------------------------------------
class RebuildBodyTestCase(_Base):
    def test_rebuild_concorrente_preserva_corpo_do_add(self) -> None:
        for round_index in range(8):
            body_file = self.home / f"body-{round_index}.md"
            body_file.write_text(f"# Demanda\n\n{MARKER} {round_index}\n", encoding="utf-8")

            rebuild = self.start_atlas("rebuild")
            added = self.run_atlas(
                "add",
                "--area",
                "projetos",
                "--title",
                f"Demanda concorrente {round_index}",
                "--body-file",
                str(body_file),
            )
            rebuild_out, rebuild_err = rebuild.communicate(timeout=120)

            self.assertEqual(rebuild.returncode, 0, rebuild_err)
            self.assertEqual(added.returncode, 0, added.stderr)
            demand_id = added.stdout.split()[0]
            body = render.read_body(self.store.demand_md(demand_id))
            self.assertIn(f"{MARKER} {round_index}", body, rebuild_out)


if __name__ == "__main__":
    unittest.main()
