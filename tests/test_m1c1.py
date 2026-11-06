"""Testes do M1c.1: pausa operacional, saúde e backup."""

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
from types import SimpleNamespace
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
BIN = REPO / "bin" / "atlas"
TMP_ROOT = REPO / "tests" / ".tmp"
NOW = "2026-11-01T14:00:00-03:00"
LATER = "2026-11-02T14:00:00-03:00"

sys.path.insert(0, str(REPO))

from atlas_cli import backup, control, health, imap_reader, outbox  # noqa: E402
from atlas_cli.store import Store  # noqa: E402
from atlas_cli.util import AtlasError, write_jsonl  # noqa: E402


class FakeImap:
    """Conta `select`; qualquer chamada prova que o reader não devia rodar."""

    def __init__(self) -> None:
        self.selected: list[tuple] = []

    def select(self, mailbox: str, readonly: bool = True):
        self.selected.append((mailbox, readonly))
        return ("OK", [b"1"])


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.home = Path(tempfile.mkdtemp(prefix="atlas-m1c1-", dir=TMP_ROOT))
        self.store = Store(home=self.home, now=NOW)
        self.store.init()

    def tearDown(self) -> None:
        shutil.rmtree(self.home, ignore_errors=True)

    def checks(self, report: dict) -> dict[str, dict]:
        return {check["name"]: check for check in report["checks"]}

    def notification(self, ident: str, status: str, created_at: str = NOW) -> dict:
        return {
            "id": ident,
            "digest_id": "dgd-0001",
            "kind": "digest",
            "created_at": created_at,
            "items": [],
            "status": status,
            "attempts": 0,
            "message": "",
        }

    def delivery(self, ident: str, state: str, lease_until: str | None = None) -> dict:
        return {
            "id": ident,
            "trigger_id": "trg-0001",
            "demand_id": "dmd-0001",
            "role": "owner",
            "state": state,
            "lease_owner": "owner" if state == "delivered" else None,
            "lease_until": lease_until,
            "lease_epoch": 1,
            "created_at": NOW,
            "updated_at": NOW,
            "error": None,
        }


class ControlTestCase(_Base):
    def test_defaults_sem_arquivo(self) -> None:
        self.assertEqual(
            control.read_control(self.store),
            {"paused": False, "reason": None, "updated_at": None},
        )
        self.assertFalse(control.is_paused(self.store))
        self.assertFalse(control.path(self.store).exists())

    def test_pause_resume_gravam_timestamp_e_reason(self) -> None:
        paused = control.pause(self.store, "  manutencao  ")
        self.assertIs(paused["paused"], True)
        self.assertEqual(paused["reason"], "manutencao")
        self.assertEqual(paused["updated_at"], NOW)
        self.assertTrue(control.is_paused(self.store))
        self.assertEqual(control.read_control(self.store), paused)

        on_disk = json.loads(control.path(self.store).read_text(encoding="utf-8"))
        self.assertEqual(on_disk, paused)
        self.assertEqual(control.path(self.store).name, "control.json")

        resumed = control.resume(self.store, now=LATER)
        self.assertEqual(resumed, {"paused": False, "reason": None, "updated_at": LATER})
        self.assertFalse(control.is_paused(self.store))

    def test_pause_sem_reason(self) -> None:
        paused = control.pause(self.store, "")
        self.assertIs(paused["paused"], True)
        self.assertIsNone(paused["reason"])

    def test_write_valida_tipos(self) -> None:
        for bad in (
            {"paused": "sim"},
            {"paused": False, "reason": 3},
            {"paused": False, "updated_at": 4},
            {"paused": False, "updated_at": "ontem"},
            "não é dict",
        ):
            with self.assertRaises(AtlasError):
                control.write_control(self.store, bad)

        control.path(self.store).write_text("{quebrado", encoding="utf-8")
        with self.assertRaises(AtlasError) as ctx:
            control.read_control(self.store)
        self.assertIn("control inválido", str(ctx.exception))

    def test_assert_active(self) -> None:
        control.assert_active(self.store, "reader")  # ativo: não levanta
        control.pause(self.store, "manutencao")
        with self.assertRaises(AtlasError) as ctx:
            control.assert_active(self.store, "reader")
        self.assertEqual(
            str(ctx.exception), "atlas pausado: reader bloqueado (manutencao)"
        )
        self.assertEqual(ctx.exception.code, 2)

        control.pause(self.store, None)
        with self.assertRaises(AtlasError) as ctx:
            control.assert_active(self.store, "outbox")
        self.assertEqual(str(ctx.exception), "atlas pausado: outbox bloqueado")


class ReaderPauseTestCase(_Base):
    def test_ingest_pausado_nao_chama_select_nem_grava_cursor(self) -> None:
        control.pause(self.store, "manutencao")
        client = FakeImap()

        with self.assertRaises(AtlasError) as ctx:
            imap_reader.ingest_mailbox(
                self.store, client, account="usuario", mailbox="INBOX"
            )

        self.assertIn("atlas pausado: reader", str(ctx.exception))
        self.assertEqual(client.selected, [])
        self.assertFalse((self.store.state_dir / "cursors").exists())

    def test_ingest_apos_resume_volta_a_ler(self) -> None:
        control.pause(self.store, "manutencao")
        control.resume(self.store)
        client = FakeImap()
        client.uid = lambda *args: ("OK", [b""])
        client.capability = lambda: ("OK", [b"IMAP4rev1"])
        client.response = lambda name: ("UIDVALIDITY", [b"7"])

        result = imap_reader.ingest_mailbox(
            self.store, client, account="usuario", mailbox="INBOX"
        )

        self.assertEqual(client.selected, [("INBOX", True)])
        self.assertEqual(result["fetched"], 0)


class OutboxPauseTestCase(_Base):
    def test_retry_pausado_nao_envia_mas_lista(self) -> None:
        write_jsonl(
            self.store.notifications_path,
            [self.notification("ntf-0001", "failed")],
        )
        control.pause(self.store, "manutencao")
        calls: list[str] = []

        def sender(message, payload):
            calls.append(message)
            return "msg-1"

        with self.assertRaises(AtlasError) as ctx:
            outbox.run(self.store, retry=True, sender=sender)
        self.assertIn("atlas pausado: outbox", str(ctx.exception))
        self.assertEqual(calls, [])

        rows = self.store.notifications()
        self.assertEqual(rows[0]["status"], "failed")

        code, listing = outbox.run(self.store, retry=False)
        self.assertEqual(code, 0)
        self.assertIn("ntf-0001 | failed", listing)

    def test_retry_apos_resume_envia(self) -> None:
        write_jsonl(
            self.store.notifications_path,
            [self.notification("ntf-0001", "failed")],
        )
        control.pause(self.store, "manutencao")
        control.resume(self.store)

        code, message = outbox.run(
            self.store, retry=True, sender=lambda message, payload: "msg-1"
        )

        self.assertEqual(code, 0)
        self.assertIn("ntf-0001 reenviado (msg-1)", message)
        self.assertEqual(self.store.notifications()[0]["status"], "sent")


class HealthTestCase(_Base):
    def report(self) -> dict:
        return health.collect(self.store)

    def test_controle_pausado_e_render(self) -> None:
        control.pause(self.store, "manutencao")
        report = self.report()

        control_check = self.checks(report)["control"]
        self.assertEqual(control_check["severity"], "info")
        self.assertIs(control_check["ok"], True)
        self.assertIn("manutencao", control_check["detail"])
        self.assertIs(report["paused"], True)
        self.assertIs(report["ok"], True)

        text = health.render(report)
        self.assertEqual(text.splitlines()[0], "PAUSADO")
        self.assertEqual(len(text.splitlines()), 7)

        control.resume(self.store)
        self.assertNotIn("PAUSADO", health.render(self.report()))

    def test_cursor_velho_e_com_erro(self) -> None:
        cursor = imap_reader.empty_cursor("usuario", "INBOX")
        cursor["last_seen_at"] = "2026-11-01T10:00:00-03:00"
        cursor["consecutive_errors"] = 2
        cursor["last_error"] = "parse falhou"
        imap_reader.replace_cursor_snapshot(self.store, cursor)

        check = self.checks(self.report())["cursors"]
        self.assertEqual(check["severity"], "warn")
        self.assertIn("usuario/INBOX", check["detail"])
        self.assertIn("parse falhou", check["detail"])
        self.assertIn("4.0h", check["detail"])

    def test_cursor_recente_sem_erro_e_info(self) -> None:
        cursor = imap_reader.empty_cursor("usuario", "INBOX")
        cursor["last_seen_at"] = "2026-11-01T13:30:00-03:00"
        imap_reader.replace_cursor_snapshot(self.store, cursor)
        self.assertEqual(self.checks(self.report())["cursors"]["severity"], "info")

    def test_cursor_invalido_e_error(self) -> None:
        path = self.store.state_dir / "cursors" / "imap-a-b.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{nao json", encoding="utf-8")
        report = self.report()
        self.assertEqual(self.checks(report)["cursors"]["severity"], "error")
        self.assertIs(report["ok"], False)

    def test_cursor_com_campo_corrompido_vira_error(self) -> None:
        cursor = imap_reader.empty_cursor("usuario", "INBOX")
        cursor["last_seen_at"] = NOW
        cursor["consecutive_errors"] = "muitos"
        imap_reader.replace_cursor_snapshot(self.store, cursor)
        report = self.report()
        self.assertEqual(self.checks(report)["cursors"]["severity"], "error")
        self.assertIs(report["ok"], False)

    def test_outbox_failed_delivery_unknown_e_pending_velho(self) -> None:
        write_jsonl(
            self.store.notifications_path,
            [
                self.notification("ntf-0001", "failed"),
                self.notification("ntf-0002", "delivery_unknown"),
                self.notification("ntf-0003", "pending", "2026-10-30T14:00:00-03:00"),
            ],
        )

        report = self.report()
        check = self.checks(report)["outbox"]
        self.assertEqual(check["severity"], "error")
        self.assertIn("failed=1 (ntf-0001)", check["detail"])
        self.assertIn("delivery_unknown=1 (ntf-0002)", check["detail"])
        self.assertIn("ntf-0003", check["detail"])
        self.assertIs(report["ok"], False)

    def test_outbox_pending_velho_isola_warn(self) -> None:
        write_jsonl(
            self.store.notifications_path,
            [self.notification("ntf-0001", "pending", "2026-10-30T14:00:00-03:00")],
        )
        report = self.report()
        self.assertEqual(self.checks(report)["outbox"]["severity"], "warn")
        self.assertIs(report["ok"], True)

    def test_outbox_recente_sem_pendencia(self) -> None:
        write_jsonl(
            self.store.notifications_path,
            [self.notification("ntf-0001", "sent")],
        )
        self.assertEqual(self.checks(self.report())["outbox"]["severity"], "info")

    def test_deliveries_lease_vencido_e_failed(self) -> None:
        write_jsonl(
            self.store.deliveries_path,
            [
                self.delivery("dlv-0001", "delivered", "2026-11-01T13:00:00-03:00"),
                self.delivery("dlv-0002", "failed"),
            ],
        )
        check = self.checks(self.report())["deliveries"]
        self.assertEqual(check["severity"], "warn")
        self.assertIn("dlv-0001", check["detail"])
        self.assertIn("dlv-0002", check["detail"])

    def test_deliveries_lease_futuro_e_info(self) -> None:
        write_jsonl(
            self.store.deliveries_path,
            [self.delivery("dlv-0001", "delivered", "2026-11-01T15:00:00-03:00")],
        )
        self.assertEqual(self.checks(self.report())["deliveries"]["severity"], "info")

    def test_backup_ausente_e_velho(self) -> None:
        self.assertEqual(self.checks(self.report())["backup"]["severity"], "warn")

        backup.create(self.store, now="2026-10-29T14:00:00-03:00")
        check = self.checks(self.report())["backup"]
        self.assertEqual(check["severity"], "warn")
        self.assertIn("velho", check["detail"])

        backup.create(self.store, now=NOW)
        self.assertEqual(self.checks(self.report())["backup"]["severity"], "info")

    def test_manifest_corrompido_cai_para_mtime(self) -> None:
        result = backup.create(self.store, now=NOW)
        manifest = Path(result["manifest"])
        for content in ('{"created_at": "ontem"}', "[]"):
            manifest.write_text(content, encoding="utf-8")
            check = self.checks(self.report())["backup"]
            self.assertEqual(check["severity"], "info")
            self.assertIn("atlas-backup-20261101T140000.tar.gz", check["detail"])

    def test_disco_baixo_e_mockado(self) -> None:
        with mock.patch(
            "atlas_cli.health.shutil.disk_usage",
            return_value=SimpleNamespace(free=1024**3),
        ):
            check = self.checks(self.report())["disk"]
        self.assertEqual(check["severity"], "warn")
        self.assertIn("< 2 GiB", check["detail"])

        with mock.patch(
            "atlas_cli.health.shutil.disk_usage",
            return_value=SimpleNamespace(free=8 * 1024**3),
        ):
            check = self.checks(self.report())["disk"]
        self.assertEqual(check["severity"], "info")

    def test_json_deterministico(self) -> None:
        write_jsonl(
            self.store.notifications_path,
            [self.notification("ntf-0001", "failed")],
        )
        first = json.dumps(
            health.collect(self.store), ensure_ascii=False, sort_keys=True
        )
        second = json.dumps(
            health.collect(self.store), ensure_ascii=False, sort_keys=True
        )
        self.assertEqual(first, second)
        self.assertEqual(
            list(json.loads(first)), ["checks", "ok", "paused"]
        )


class BackupTestCase(_Base):
    def seed(self) -> None:
        write_jsonl(
            self.store.triggers_path,
            [{"id": "trg-0001", "source": "email", "external_id": "<a@b>"}],
        )
        blob = self.home / "raw" / "blobs" / "ab" / ("ab" + "0" * 62 + ".txt")
        blob.parent.mkdir(parents=True, exist_ok=True)
        blob.write_text("corpo bruto\n", encoding="utf-8")
        (self.home / "BULLETIN.md").write_text("# BULLETIN\n", encoding="utf-8")
        (self.home / "reports").mkdir(parents=True, exist_ok=True)
        (self.home / "reports" / "shadow.md").write_text("rel\n", encoding="utf-8")

    def test_cria_tar_manifest_e_conteudo(self) -> None:
        self.seed()
        (self.store.state_dir / "atlasd.sock").write_text("", encoding="utf-8")
        (self.store.state_dir / "hub.db-wal").write_text("wal", encoding="utf-8")
        (self.store.state_dir / "hub.db-shm").write_text("shm", encoding="utf-8")
        (self.home / "secrets.env").write_text("ATLAS_IMAP_PASS=segredo\n", encoding="utf-8")
        (self.home / "tests").mkdir(parents=True, exist_ok=True)
        (self.home / "tests" / "test_x.py").write_text("x = 1\n", encoding="utf-8")

        result = backup.create(self.store)

        tar_path = Path(result["path"])
        self.assertEqual(tar_path.name, "atlas-backup-20261101T140000.tar.gz")
        self.assertEqual(tar_path.parent, self.home / "backups")
        self.assertEqual(tar_path.parent.stat().st_mode & 0o777, 0o700)

        manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
        self.assertEqual(
            set(manifest), {"bytes", "created_at", "entries", "sha256"}
        )
        self.assertEqual(manifest["created_at"], NOW)
        self.assertEqual(manifest["entries"], result["entries"])
        self.assertEqual(manifest["bytes"], tar_path.stat().st_size)
        self.assertEqual(manifest["sha256"], result["sha256"])
        self.assertEqual(len(manifest["sha256"]), 64)

        with tarfile.open(tar_path, "r:gz") as archive:
            names = archive.getnames()
        self.assertIn("state/triggers.jsonl", names)
        self.assertIn("state/events.jsonl", names)
        self.assertIn("policy/v1.json", names)
        self.assertIn("raw/blobs/ab/ab" + "0" * 62 + ".txt", names)
        self.assertIn("BULLETIN.md", names)
        self.assertIn("reports/shadow.md", names)
        for excluded in (
            "state/atlasd.sock",
            "state/atlasd.socket",
            "state/hub.db-wal",
            "state/hub.db-shm",
            "secrets.env",
            "tests/test_x.py",
        ):
            self.assertNotIn(excluded, names)
        self.assertEqual(len(names), result["entries"])

    def test_exclui_arquivos_efemeros_do_daemon(self) -> None:
        # O daemon remove `atlasd.pid` e rotaciona `atlasd.log` fora do
        # `state/.lock`. Se entrassem no tar, um backup concorrente falharia
        # com FileNotFoundError entre a enumeração e o `archive.add`.
        self.seed()
        ephemeral = {
            "state/atlasd.pid": "12345\n",
            "state/atlasd.log": "log atual\n",
            "state/atlasd.log.1": "log rotacionado\n",
        }
        for name, content in ephemeral.items():
            (self.home / name).write_text(content, encoding="utf-8")

        # A enumeração nunca inclui os efêmeros do daemon.
        arcnames = {arcname for _, arcname in backup._entries(self.store)}
        for name in ephemeral:
            self.assertNotIn(name, arcnames)

        # Simula o daemon parando entre a enumeração e o `archive.add`: como
        # o membro jamais foi enumerado, o backup publica normalmente.
        for name in ephemeral:
            (self.home / name).unlink()

        result = backup.create(self.store)

        with tarfile.open(result["path"], "r:gz") as archive:
            names = archive.getnames()
        for name in ephemeral:
            self.assertNotIn(name, names)
        manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
        self.assertEqual(manifest["entries"], len(names))
        self.assertEqual(result["entries"], len(names))

    def test_prune_mantem_n(self) -> None:
        for hour in (0, 1, 2):
            backup.create(self.store, now=f"2026-11-01T1{hour}:00:00-03:00", keep=2)

        tars = sorted((self.home / "backups").glob("atlas-backup-*.tar.gz"))
        manifests = sorted((self.home / "backups").glob("atlas-backup-*.json"))
        self.assertEqual(
            [path.name for path in tars],
            [
                "atlas-backup-20261101T110000.tar.gz",
                "atlas-backup-20261101T120000.tar.gz",
            ],
        )
        self.assertEqual(len(manifests), 2)
        self.assertEqual(backup.latest(self.store), tars[-1])

    def test_out_dir_customizado(self) -> None:
        target = self.home / "custom"
        result = backup.create(self.store, out_dir=target, keep=3)
        self.assertEqual(Path(result["path"]).parent, target)
        self.assertTrue(Path(result["manifest"]).is_file())
        self.assertIsNone(backup.latest(self.store))

    def test_out_dir_invalido_vira_atlas_error(self) -> None:
        blocker = self.home / "arquivo"
        blocker.write_text("x", encoding="utf-8")
        with self.assertRaises(AtlasError) as ctx:
            backup.create(self.store, out_dir=blocker / "sub")
        self.assertIn("backup falhou ao preparar", str(ctx.exception))

    def test_keep_invalido(self) -> None:
        for keep in (0, -1):
            with self.assertRaises(AtlasError):
                backup.create(self.store, keep=keep)

    def test_verify_rejeita_tar_invalido(self) -> None:
        bogus = self.home / "bogus.tar.gz"
        bogus.write_text("não é tar\n", encoding="utf-8")
        with self.assertRaises(AtlasError):
            backup._verify(bogus, 1)

    def test_restaura_em_temp(self) -> None:
        self.seed()
        result = backup.create(self.store)
        target = Path(tempfile.mkdtemp(prefix="atlas-restore-", dir=TMP_ROOT))
        try:
            with tarfile.open(result["path"], "r:gz") as archive:
                archive.extractall(target)
            restored = Store(home=target, now=NOW)
            self.assertEqual(restored.triggers()[0]["id"], "trg-0001")
            blob = target / "raw" / "blobs" / "ab" / ("ab" + "0" * 62 + ".txt")
            self.assertEqual(blob.read_text(encoding="utf-8"), "corpo bruto\n")
        finally:
            shutil.rmtree(target, ignore_errors=True)

    def test_latest_vazio(self) -> None:
        self.assertIsNone(backup.latest(self.store))


class CliTestCase(_Base):
    def run_atlas(self, *args: str, now: str = NOW) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(BIN), "--now", now, *args],
            capture_output=True,
            text=True,
            env=dict(os.environ, ATLAS_HOME=str(self.home)),
            check=False,
        )

    def test_pause_resume(self) -> None:
        paused = self.run_atlas("pause", "--reason", "manutencao")
        self.assertEqual(paused.returncode, 0, paused.stderr)
        self.assertEqual(paused.stdout.strip(), "paused=true")
        self.assertTrue(control.is_paused(self.store))

        resumed = self.run_atlas("resume")
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(resumed.stdout.strip(), "paused=false")
        self.assertFalse(control.is_paused(self.store))

    def test_health_texto_json_e_exit_codes(self) -> None:
        ok = self.run_atlas("health")
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertIn("control", ok.stdout)

        self.run_atlas("pause", "--reason", "manutencao")
        paused = self.run_atlas("health")
        self.assertEqual(paused.stdout.splitlines()[0], "PAUSADO")

        report = self.run_atlas("health", "--json")
        self.assertEqual(report.returncode, 0, report.stderr)
        data = json.loads(report.stdout)
        self.assertEqual(list(data), ["checks", "ok", "paused"])
        self.assertIs(data["paused"], True)
        self.assertEqual(
            [check["name"] for check in data["checks"]],
            ["control", "cursors", "outbox", "deliveries", "backup", "disk"],
        )

        write_jsonl(
            self.store.notifications_path,
            [self.notification("ntf-0001", "failed")],
        )
        broken = self.run_atlas("health")
        self.assertEqual(broken.returncode, 1)
        self.assertIn("error", broken.stdout)

    def test_backup_imprime_resumo(self) -> None:
        proc = self.run_atlas("backup", "--keep", "3")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        line = proc.stdout.strip()
        self.assertTrue(line.startswith("backup="), line)
        self.assertIn(" entries=", line)
        self.assertIn(" bytes=", line)
        path = Path(line.split()[0].split("=", 1)[1])
        self.assertTrue(path.is_file())
        self.assertTrue(path.with_name(path.name[:-7] + ".json").is_file())


if __name__ == "__main__":
    unittest.main()
