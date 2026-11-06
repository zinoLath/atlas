"""Testes do M1b.0f-E: IMAP-04, BACKUP-02-R1, STATE-02-R1, TLS-01, BACKUP-03."""

from __future__ import annotations

import json
import shutil
import ssl
import sqlite3
import sys
import tarfile
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
TMP_ROOT = REPO / "tests" / ".tmp"
NOW = "2026-11-01T14:00:00-03:00"
TODAY = "2026-11-01"
ACCOUNT = "usuario"
MAILBOX = "INBOX"

sys.path.insert(0, str(REPO))

from atlas_cli import backup, imap_reader, kb  # noqa: E402
from atlas_cli.store import Store  # noqa: E402
from atlas_cli.util import AtlasError  # noqa: E402


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.home = Path(tempfile.mkdtemp(prefix="atlas-m1b0f-e-", dir=TMP_ROOT))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.store = Store(home=self.home, now=NOW)
        self.store.init()

    def cursor(self) -> dict:
        return imap_reader.read_cursor(self.store, ACCOUNT, MAILBOX)

    def seed_progress(self) -> dict:
        cursor = self.cursor()
        cursor["last_uid"] = 7
        cursor["bootstrap"].update(
            done=False,
            since="2026-10-18",
            unseen=True,
            high_water=50,
            pending=[41, 42, 43],
        )
        imap_reader.replace_cursor_snapshot(self.store, cursor)
        return cursor

    def run_threads(self, *targets) -> None:
        """Roda os alvos em paralelo e propaga a primeira exceção."""
        errors: list[BaseException] = []

        def wrap(target):
            def run():
                try:
                    target()
                except BaseException as exc:  # noqa: BLE001 - re-raise abaixo
                    errors.append(exc)

            return run

        threads = [
            threading.Thread(target=wrap(target), name=f"t{index}")
            for index, target in enumerate(targets)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
            self.assertFalse(thread.is_alive(), "thread travada")
        if errors:
            raise errors[0]


class CursorTelemetryTest(_Base):
    """IMAP-04: telemetria relê o cursor e preserva o progresso alheio."""

    def test_falha_concorrente_preserva_pending_de_outro_leitor(self) -> None:
        a_read = threading.Event()
        b_wrote = threading.Event()

        def leitor_a():
            # Snapshot capturado antes do connect: pode estar vazio.
            stale = self.cursor()
            self.assertEqual(stale["bootstrap"]["pending"], None)
            a_read.set()
            self.assertTrue(b_wrote.wait(10))
            imap_reader.mark_failure(
                self.store, ACCOUNT, MAILBOX, RuntimeError("sem rede")
            )

        def leitor_b():
            self.assertTrue(a_read.wait(10))
            self.seed_progress()
            b_wrote.set()

        self.run_threads(leitor_a, leitor_b)

        cursor = self.cursor()
        self.assertEqual(cursor["bootstrap"]["pending"], [41, 42, 43])
        self.assertEqual(cursor["bootstrap"]["high_water"], 50)
        self.assertEqual(cursor["last_uid"], 7)
        self.assertEqual(cursor["consecutive_errors"], 1)
        self.assertIn("sem rede", cursor["last_error"])
        self.assertEqual(cursor["last_attempt_at"], NOW)

    def test_attempt_so_toca_telemetria(self) -> None:
        self.seed_progress()
        imap_reader.mark_attempt(self.store, ACCOUNT, MAILBOX, now=NOW)

        cursor = self.cursor()
        self.assertEqual(cursor["bootstrap"]["pending"], [41, 42, 43])
        self.assertEqual(cursor["bootstrap"]["high_water"], 50)
        self.assertEqual(cursor["last_uid"], 7)
        self.assertIsNone(cursor["last_error"])
        self.assertEqual(cursor["consecutive_errors"], 0)
        self.assertEqual(cursor["last_attempt_at"], NOW)

    def test_cursor_criado_do_zero_quando_nao_existe(self) -> None:
        imap_reader.mark_failure(
            self.store, ACCOUNT, MAILBOX, RuntimeError("login recusado")
        )
        cursor = self.cursor()
        self.assertEqual(cursor["account"], ACCOUNT)
        self.assertEqual(cursor["mailbox"], MAILBOX)
        self.assertEqual(cursor["consecutive_errors"], 1)
        self.assertIn("login recusado", cursor["last_error"])


class BackupTmpTest(_Base):
    """BACKUP-02-R1: temporários de publicadores nunca entram no tar."""

    def test_tmp_em_qualquer_arvore_fica_fora(self) -> None:
        temps = [
            self.home / "state" / "hub.db.deadbeef.tmp",
            self.home / "state" / "demands.jsonl.abc123.tmp",
            self.home / "raw" / "blobs" / "sha.deadbeef.tmp",
            self.home / "reports" / "shadow" / f"{TODAY}.md.abc123.tmp",
            self.home / "backups" / ".atlas-backup-20261101T140000.tmp",
        ]
        for index, path in enumerate(temps):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"temporario {index}", encoding="utf-8")

        result = backup.create(self.store)

        with tarfile.open(result["path"], "r:gz") as archive:
            names = archive.getnames()
        self.assertFalse([name for name in names if name.endswith(".tmp")])
        self.assertEqual(result["entries"], len(names))
        manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
        self.assertEqual(manifest["entries"], len(names))

    def test_membro_canonico_que_some_derruba_backup(self) -> None:
        # Um membro canônico enumerado só pode desaparecer por violação de
        # contrato: escritores publicam sob o mesmo lock do backup e blobs
        # são content-addressed (imutáveis). Ignorar o sumiço publicaria um
        # tar/manifest de sucesso com menos membros que o real.
        blob_dir = self.home / "raw" / "blobs"
        blob_dir.mkdir(parents=True, exist_ok=True)
        kept = blob_dir / "sha-kept"
        kept.write_text("blob real", encoding="utf-8")
        ghost = blob_dir / "sha-ghost"
        ghost.write_text("vai sumir", encoding="utf-8")

        # Um backup anterior não pode ser substituído por um incompleto e
        # nenhum `.tmp` pode ficar órfão após a falha.
        directory = backup.default_dir(self.store)
        directory.mkdir(parents=True, exist_ok=True)
        stamp = self.store.dt.strftime("%Y%m%dT%H%M%S")
        tar_path = directory / f"atlas-backup-{stamp}.tar.gz"
        tar_path.write_bytes(b"tar antigo intacto")
        manifest_path = backup._manifest_path(tar_path)
        manifest_path.write_text('{"antigo": true}', encoding="utf-8")

        # Enumeração viu o ghost; ele é removido antes do `tar.add` abrir.
        stale_pairs = [(kept, "raw/blobs/sha-kept"), (ghost, "raw/blobs/sha-ghost")]
        ghost.unlink()

        with mock.patch.object(backup, "_entries", return_value=stale_pairs):
            with self.assertRaises(AtlasError):
                backup.create(self.store)

        self.assertEqual(tar_path.read_bytes(), b"tar antigo intacto")
        self.assertEqual(
            manifest_path.read_text(encoding="utf-8"), '{"antigo": true}'
        )
        self.assertFalse(
            [path for path in directory.iterdir() if path.name.endswith(".tmp")]
        )


class SqliteUnderLockTest(_Base):
    """STATE-02-R1: todo publicador do SQLite obedece ao lock comum."""

    def demands(self, count: int) -> None:
        with self.store.transaction("test.demand") as tx:
            for index in range(count):
                tx.upsert_demand(
                    {
                        "id": f"dem-{index}",
                        "area": "projetos",
                        "state": "new",
                        "title": f"Demanda {index}",
                        "updated_at": NOW,
                        "due": None,
                    }
                )

    def test_index_reconstroi_db_sob_lock(self) -> None:
        self.demands(1)
        with self.store.transaction("test.kb") as tx:
            kb.add_node(tx, parent="", kind="area", title="Internet", summary="")

        depths: list[int] = []
        original = Store._rebuild_db_unlocked

        def spy(store):
            depths.append(store._lock_depth)
            return original(store)

        with mock.patch.object(Store, "_rebuild_db_unlocked", spy):
            kb.index(self.store)

        self.assertEqual(depths, [1])
        self.assertTrue((self.home / "INDEX.md").is_file())
        conn = sqlite3.connect(self.store.db_path)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM demands").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM kb_nodes").fetchone()[0], 1)
        finally:
            conn.close()

    def test_rebuild_db_sob_lock_nao_reabre_flock(self) -> None:
        self.demands(1)
        with self.store.lock():
            self.assertEqual(self.store._lock_depth, 1)
            self.store.rebuild_db()
            self.assertEqual(self.store._lock_depth, 1)
        self.assertEqual(self.store._lock_depth, 0)

    def test_rebuild_db_concorrente_usa_temp_exclusivo(self) -> None:
        self.demands(1)
        paths: list[str] = []
        original = Store.connect

        def spy(store, path=None):
            paths.append(str(path or store.db_path))
            return original(store, path)

        other = Store(home=self.home, now=NOW)
        with mock.patch.object(Store, "connect", spy):
            self.run_threads(self.store.rebuild_db, other.rebuild_db)

        temps = [
            path
            for path in paths
            if path != str(self.store.db_path) and path.endswith(".tmp")
        ]
        self.assertEqual(len(temps), 2)
        self.assertEqual(len(set(temps)), 2)
        self.assertFalse((self.store.state_dir / "hub.db.tmp").exists())

    def test_index_e_rebuild_concorrentes_mantem_sqlite_integro(self) -> None:
        self.demands(3)

        def indexar():
            for _ in range(5):
                kb.index(self.store)

        def reconstruir():
            for _ in range(5):
                self.store.rebuild()

        self.run_threads(indexar, reconstruir)

        conn = sqlite3.connect(self.store.db_path)
        try:
            self.assertEqual(
                conn.execute("PRAGMA integrity_check").fetchone()[0], "ok"
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM demands").fetchone()[0], 3
            )
            ids = [row[0] for row in conn.execute("SELECT id FROM demands ORDER BY id")]
        finally:
            conn.close()
        self.assertEqual(ids, ["dem-0", "dem-1", "dem-2"])


class TlsTest(_Base):
    """TLS-01: o cliente verifica hostname e cadeia do servidor."""

    def test_contexto_padrao_verifica_hostname_e_certificado(self) -> None:
        config = {
            "host": "imap.example.com",
            "port": 993,
            "user": "eu@example.com",
            "password": "segredo",
        }
        conn = mock.MagicMock()
        conn.login.return_value = ("OK", [b"ok"])
        with mock.patch.object(
            imap_reader.imaplib, "IMAP4_SSL", return_value=conn
        ) as factory:
            imap_reader.RealImapClient(config)

        factory.assert_called_once()
        args, kwargs = factory.call_args
        self.assertEqual(args, ("imap.example.com", 993))
        self.assertEqual(kwargs["timeout"], imap_reader.FETCH_TIMEOUT_SECONDS)
        context = kwargs["ssl_context"]
        self.assertIsInstance(context, ssl.SSLContext)
        self.assertIs(context.check_hostname, True)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)


class ShadowReportLockTest(_Base):
    """BACKUP-03: o relatório shadow é publicado sob o lock dos escritores."""

    def test_reports_shadow_escrito_sob_lock(self) -> None:
        depths: list[int] = []
        original = imap_reader.write_text

        def spy(path, text):
            depths.append(self.store._lock_depth)
            return original(path, text)

        with mock.patch.object(imap_reader, "write_text", spy):
            text = imap_reader.shadow_report(self.store, date=TODAY)

        self.assertEqual(depths, [1])
        path = self.home / "reports" / "shadow" / f"{TODAY}.md"
        self.assertEqual(path.read_text(encoding="utf-8"), text)

    def test_backup_concorrente_com_relatorio_nao_mistura_versoes(self) -> None:
        # Um relatório já publicado garante que o tar sempre tem o membro.
        imap_reader.shadow_report(self.store, date=TODAY)

        def escrever_relatorios():
            for _ in range(10):
                imap_reader.shadow_report(self.store, date=TODAY)

        result: dict = {}

        def criar_backup():
            result.update(backup.create(self.store))

        self.run_threads(escrever_relatorios, criar_backup)

        with tarfile.open(result["path"], "r:gz") as archive:
            members = [
                item
                for item in archive.getmembers()
                if item.name.startswith("reports/shadow/")
            ]
            for member in members:
                payload = archive.extractfile(member).read().decode("utf-8")
                self.assertTrue(payload.startswith(f"# SHADOW {TODAY}\n"))
                self.assertIn("- mensagens lidas: 0", payload)
                self.assertTrue(payload.endswith("\n"))
                self.assertNotIn("\x00", payload)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
