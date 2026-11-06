"""Testes do M1c.3: sender Signal via signal-cli."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
TMP_ROOT = REPO / "tests" / ".tmp"
NOW = "2026-11-01T14:00:00-03:00"
LATER = "2026-11-01T14:05:00-03:00"
NUMBER = "+5511999990000"
TO = "+5511888880000"
MESSAGE = "ATLAS 14:00 | 1 item pendente"

sys.path.insert(0, str(REPO))

from atlas_cli import signal as signal_sender  # noqa: E402
from atlas_cli.cli import main as atlas_main  # noqa: E402
from atlas_cli.store import Store  # noqa: E402
from atlas_cli.util import AtlasError, write_jsonl  # noqa: E402

ENV_KEYS = (
    "ATLAS_SIGNAL_NUMBER",
    "ATLAS_SIGNAL_TO",
    "ATLAS_SIGNAL_CLI",
    "ATLAS_SIGNAL_CONFIG",
)


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.home = Path(tempfile.mkdtemp(prefix="atlas-m1c3-", dir=TMP_ROOT))
        self.env_path = self.home / "secrets.env"
        self.cli_dir = self.home / "signal-cli-data"
        self._saved_env = {key: os.environ.get(key) for key in ENV_KEYS}
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        self._env_path_patch = mock.patch.object(
            signal_sender, "DEFAULT_ENV_PATH", self.env_path
        )
        self._env_path_patch.start()

    def tearDown(self) -> None:
        self._env_path_patch.stop()
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.home, ignore_errors=True)

    def write_env(self, text: str) -> None:
        self.env_path.write_text(text, encoding="utf-8")

    def base_env(self) -> dict[str, str]:
        return {
            "ATLAS_SIGNAL_NUMBER": NUMBER,
            "ATLAS_SIGNAL_CONFIG": str(self.cli_dir),
        }

    def run_signal(self, *, stdout: str = "123\n", stderr: str = "", rc: int = 0):
        result = SimpleNamespace(returncode=rc, stdout=stdout, stderr=stderr)
        return mock.patch.object(subprocess, "run", return_value=result)


class ConfigTestCase(_Base):
    def test_load_number_env_vence_arquivo(self) -> None:
        self.write_env(f"ATLAS_SIGNAL_NUMBER=+0000\n")
        os.environ["ATLAS_SIGNAL_NUMBER"] = NUMBER
        self.assertEqual(signal_sender.load_number(), NUMBER)

    def test_load_number_le_secrets_env(self) -> None:
        self.write_env(f"# comentário\nexport ATLAS_SIGNAL_NUMBER='{NUMBER}'\n")
        self.assertEqual(signal_sender.load_number(self.env_path), NUMBER)

    def test_load_number_ausente_falha_sem_ecoar(self) -> None:
        self.write_env("ATLAS_SIGNAL_TO=+5511000000000\n")
        with self.assertRaises(AtlasError) as ctx:
            signal_sender.load_number()
        self.assertIn("ATLAS_SIGNAL_NUMBER", str(ctx.exception))
        self.assertNotIn("+5511000000000", str(ctx.exception))


class SendTestCase(_Base):
    def test_send_note_to_self_argv_exato(self) -> None:
        os.environ.update(self.base_env())
        patcher = self.run_signal(stdout="id-42\n")
        with patcher as run:
            message_id = signal_sender.send(MESSAGE, {"id": "dgd-0001"})

        self.assertEqual(message_id, "signal:id-42")
        run.assert_called_once()
        argv = run.call_args.args[0]
        self.assertEqual(
            argv,
            [
                "signal-cli",
                "-a",
                NUMBER,
                "--config",
                str(self.cli_dir),
                "send",
                "--note-to-self",
                "-m",
                MESSAGE,
            ],
        )
        kwargs = run.call_args.kwargs
        self.assertEqual(
            kwargs,
            {
                "capture_output": True,
                "text": True,
                "timeout": 60,
                "check": False,
            },
        )

    def test_send_com_destinatario_omite_note_to_self(self) -> None:
        os.environ.update(self.base_env())
        os.environ["ATLAS_SIGNAL_TO"] = TO
        patcher = self.run_signal(stdout="")
        with patcher as run:
            message_id = signal_sender.send(MESSAGE, {"id": "dgd-0001"})

        self.assertEqual(message_id, "signal:ok")
        argv = run.call_args.args[0]
        self.assertNotIn("--note-to-self", argv)
        self.assertEqual(argv[-3:], ["-m", MESSAGE, TO])

    def test_send_cli_customizado(self) -> None:
        os.environ.update(self.base_env())
        os.environ["ATLAS_SIGNAL_CLI"] = "/opt/bin/signal-cli"
        patcher = self.run_signal()
        with patcher as run:
            signal_sender.send(MESSAGE, {"id": "dgd-0001"})
        self.assertEqual(run.call_args.args[0][0], "/opt/bin/signal-cli")

    def test_rc_diferente_de_zero_vira_atlas_error_sem_vazar(self) -> None:
        os.environ.update(self.base_env())
        os.environ["ATLAS_SIGNAL_TO"] = TO
        stderr = f"Failed to send {MESSAGE} from {NUMBER} to {TO}\n"
        patcher = self.run_signal(rc=2, stderr=stderr)
        with patcher:
            with self.assertRaises(AtlasError) as ctx:
                signal_sender.send(MESSAGE, {"id": "dgd-0001"})

        text = str(ctx.exception)
        self.assertIn("rc=2", text)
        self.assertNotIn(MESSAGE, text)
        self.assertNotIn(NUMBER, text)
        self.assertNotIn(TO, text)
        self.assertIn("<redigido>", text)

    def test_numero_formatado_no_stderr_tambem_e_redigido(self) -> None:
        os.environ.update(self.base_env())
        os.environ["ATLAS_SIGNAL_TO"] = TO
        stderr = f"failed: from +55 11 99999-0000 to +55 (11) 88888-0000 {MESSAGE}\n"
        patcher = self.run_signal(rc=1, stderr=stderr)
        with patcher:
            with self.assertRaises(AtlasError) as ctx:
                signal_sender.send(MESSAGE, {"id": "dgd-0001"})

        text = str(ctx.exception)
        self.assertIn("rc=1", text)
        self.assertNotIn("99999", text)
        self.assertNotIn("0000", text)
        self.assertNotIn(MESSAGE, text)

    def test_binario_ausente_vira_atlas_error(self) -> None:
        os.environ.update(self.base_env())
        with mock.patch.object(subprocess, "run", side_effect=FileNotFoundError):
            with self.assertRaises(AtlasError) as ctx:
                signal_sender.send(MESSAGE, {"id": "dgd-0001"})
        self.assertIn("não encontrado", str(ctx.exception))
        self.assertNotIn(MESSAGE, str(ctx.exception))

    def test_timeout_devolve_unknown_sem_vazar_mensagem(self) -> None:
        os.environ.update(self.base_env())
        expired = subprocess.TimeoutExpired([MESSAGE], 60, output=MESSAGE)
        with mock.patch.object(subprocess, "run", side_effect=expired):
            result = signal_sender.send(MESSAGE, {"id": "dgd-0001"})
        self.assertEqual(result, "unknown")

    def test_sem_numero_falha_antes_de_chamar_o_binario(self) -> None:
        with mock.patch.object(subprocess, "run") as run:
            with self.assertRaises(AtlasError):
                signal_sender.send(MESSAGE, {"id": "dgd-0001"})
        run.assert_not_called()


class OutboxSignalTestCase(_Base):
    def setUp(self) -> None:
        super().setUp()
        self._saved_home = os.environ.get("ATLAS_HOME")
        os.environ["ATLAS_HOME"] = str(self.home)
        self.store = Store(home=self.home, now=NOW)
        self.store.init()

    def tearDown(self) -> None:
        if self._saved_home is None:
            os.environ.pop("ATLAS_HOME", None)
        else:
            os.environ["ATLAS_HOME"] = self._saved_home
        super().tearDown()

    def notification(self, status: str = "failed") -> dict:
        return {
            "id": "ntf-0001",
            "digest_id": "dgd-0001",
            "kind": "digest",
            "created_at": NOW,
            "items": [],
            "payload_hash": "sha256:0",
            "status": status,
            "attempts": 1,
            "sent_at": None,
            "message_id": None,
            "error": "discagem falhou",
            "upper_seq": 0,
            "displayed": 0,
            "hidden": 0,
            "message": MESSAGE,
        }

    def write_notification(self, status: str = "failed") -> None:
        write_jsonl(self.store.notifications_path, [self.notification(status)])

    def rows(self) -> list[dict]:
        text = self.store.notifications_path.read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    def test_retry_com_sender_signal_grava_sent(self) -> None:
        self.write_notification("failed")
        os.environ.update(self.base_env())
        patcher = self.run_signal(stdout="sig-77\n")
        with patcher as run:
            code = atlas_main(["--now", LATER, "outbox", "--retry", "--sender", "signal"])

        self.assertEqual(code, 0)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][-1], MESSAGE)
        row = self.rows()[0]
        self.assertEqual(row["status"], "sent")
        self.assertEqual(row["message_id"], "signal:sig-77")
        self.assertEqual(row["attempts"], 2)
        self.assertEqual(row["message"], MESSAGE)

    def test_falha_do_signal_marca_failed_sem_perder_notificacao(self) -> None:
        self.write_notification("failed")
        os.environ.update(self.base_env())
        patcher = self.run_signal(rc=1, stderr="network down")
        with patcher:
            code = atlas_main(["--now", LATER, "outbox", "--retry", "--sender", "signal"])

        self.assertEqual(code, 1)
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "failed")
        self.assertIn("signal-cli falhou", rows[0]["error"])
        self.assertIn("network down", rows[0]["error"])
        self.assertEqual(rows[0]["message"], MESSAGE)
        self.assertIsNone(rows[0]["sent_at"])

    def test_sinal_sem_numero_nao_explode_o_comando(self) -> None:
        self.write_notification("failed")
        with mock.patch.object(subprocess, "run") as run:
            code = atlas_main(["--now", LATER, "outbox", "--retry", "--sender", "signal"])

        self.assertEqual(code, 1)
        run.assert_not_called()
        row = self.rows()[0]
        self.assertEqual(row["status"], "failed")
        self.assertIn("ATLAS_SIGNAL_NUMBER", row["error"])

    def test_timeout_marca_delivery_unknown_e_nao_reenvia(self) -> None:
        self.write_notification("failed")
        os.environ.update(self.base_env())
        expired = subprocess.TimeoutExpired([MESSAGE], 60, output=MESSAGE)
        with mock.patch.object(subprocess, "run", side_effect=expired) as run:
            code = atlas_main(["--now", LATER, "outbox", "--retry", "--sender", "signal"])
            self.assertEqual(code, 1)
            self.assertEqual(run.call_count, 1)
            row = self.rows()[0]
            self.assertEqual(row["status"], "delivery_unknown")
            self.assertIsNone(row["error"])
            self.assertIsNone(row["message_id"])
            self.assertIsNone(row["sent_at"])
            self.assertEqual(row["message"], MESSAGE)

            # Sem --force o retry não repete uma entrega ambígua.
            code = atlas_main(["--now", LATER, "outbox", "--retry", "--sender", "signal"])
        self.assertEqual(code, 0)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(self.rows()[0]["status"], "delivery_unknown")

    def test_sender_stdout_continua_padrao(self) -> None:
        self.write_notification("failed")
        code = atlas_main(["--now", LATER, "outbox", "--retry"])
        self.assertEqual(code, 0)
        row = self.rows()[0]
        self.assertEqual(row["status"], "sent")
        self.assertEqual(row["message_id"], "stdout:dgd-0001")


if __name__ == "__main__":
    unittest.main()
