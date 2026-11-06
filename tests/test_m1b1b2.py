"""Testes do M1b.1b2: reader IMAP read-only, cursor e relatório de shadow."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
TMP_ROOT = REPO / "tests" / ".tmp"
NOW = "2026-11-01T14:00:00-03:00"
TODAY = "2026-11-01"

sys.path.insert(0, str(REPO))

from atlas_cli import imap_reader  # noqa: E402
from atlas_cli.store import Store  # noqa: E402
from atlas_cli.util import AtlasError, write_jsonl  # noqa: E402


def raw_email(
    *,
    message_id: str | None = "<msg@example.com>",
    subject: str = "Fatura Nubank",
    sender: str = "no-reply@nubank.com.br",
    body: str = "corpo da mensagem",
    date: str = "Sat, 01 Nov 2026 12:00:00 -0300",
) -> bytes:
    lines = [f"From: {sender}", "To: eu@example.com", f"Subject: {subject}"]
    if message_id is not None:
        lines.append(f"Message-ID: {message_id}")
    lines.extend([f"Date: {date}", "MIME-Version: 1.0"])
    lines.append('Content-Type: text/plain; charset="utf-8"')
    lines.append("")
    lines.append(body)
    return "\r\n".join(lines).encode("utf-8")


class FakeImap:
    """Fake mínimo com a mesma forma de resposta do `imaplib`."""

    def __init__(
        self,
        *,
        uidvalidity: int = 123,
        capabilities=("IMAP4rev1",),
        messages=(),
    ) -> None:
        self.uidvalidity = uidvalidity
        self.capabilities_list = list(capabilities)
        self.messages = sorted(
            (dict(item) for item in messages), key=lambda item: item["uid"]
        )
        self.selected: list[tuple] = []
        self.searches: list[tuple] = []
        self.fetch_args: list[tuple] = []
        self.fail_fetch: set[int] = set()
        self.fail_uidvalidity = False
        self.uidnext = "auto"

    def select(self, mailbox: str, readonly: bool = True):
        self.selected.append((mailbox, readonly))
        return ("OK", [b"1"])

    def response(self, name: str):
        if name == "UIDNEXT":
            value = self.uidnext
            if value == "auto":
                value = self.messages[-1]["uid"] + 1 if self.messages else 1
            if value is None:
                return ("OK", [None])
            return ("UIDNEXT", [str(value).encode()])
        if name != "UIDVALIDITY":
            return ("OK", [None])
        if self.fail_uidvalidity:
            return ("NO", [None])
        # `imaplib.IMAP4.response` devolve a chave untagged com que o servidor
        # respondeu ao SELECT, não "OK".
        return ("UIDVALIDITY", [str(self.uidvalidity).encode()])

    def capability(self):
        return ("OK", [" ".join(self.capabilities_list).encode()])

    def uid(self, command: str, *args):
        command = command.upper()
        if command == "SEARCH":
            self.searches.append(tuple(str(arg) for arg in args))
            uids = [item["uid"] for item in self.messages]
            criteria = args[0] if args else None
            if criteria in ("SINCE", "ALL"):
                pass
            elif criteria == "UNSEEN":
                uids = [
                    item["uid"] for item in self.messages if item.get("unseen", True)
                ]
            else:
                spec = str(args[1])
                if ":" not in spec:
                    exact = int(spec)
                    uids = [uid for uid in uids if uid == exact]
                else:
                    lower = int(spec.split(":")[0])
                    if uids and lower > uids[-1]:
                        # RFC 3501: `n:*` com n maior que o último ainda
                        # devolve o último UID — é o reader quem filtra.
                        uids = [uids[-1]]
                    else:
                        uids = [uid for uid in uids if uid >= lower]
            return ("OK", [" ".join(str(uid) for uid in uids).encode()])
        if command == "FETCH":
            self.fetch_args.append(tuple(str(arg) for arg in args))
            uid = int(args[0])
            if uid in self.fail_fetch:
                return ("BAD", [b"fetch falhou"])
            message = next(
                (item for item in self.messages if item["uid"] == uid), None
            )
            if message is None:
                return ("OK", [None])
            raw = message["raw"]
            head = f"1 (UID {uid} RFC822 {{{len(raw)}}}".encode()
            data: list = [(head, raw)]
            if "X-GM-MSGID" in str(args[1]) and message.get("x_gm_msgid"):
                data.append(
                    (
                        f" X-GM-MSGID {message['x_gm_msgid']} "
                        f"X-GM-THRID {message.get('x_gm_thrid', '0')})"
                    ).encode()
                )
            else:
                data.append(b")")
            return ("OK", data)
        return ("BAD", [b"comando desconhecido"])


class M1b1b2TestCase(unittest.TestCase):
    def setUp(self) -> None:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.home = Path(tempfile.mkdtemp(prefix="atlas-m1b1b2-", dir=TMP_ROOT))
        self.store = Store(home=self.home, now=NOW)
        self.store.init()

    def tearDown(self) -> None:
        shutil.rmtree(self.home, ignore_errors=True)

    # ---- helpers ------------------------------------------------------
    def message(self, uid: int, **raw_kwargs) -> dict:
        options = {
            "message_id": f"<msg-{uid}@example.com>",
            "subject": f"Assunto {uid}",
        }
        unseen = raw_kwargs.pop("unseen", True)
        options.update(raw_kwargs)
        return {"uid": uid, "raw": raw_email(**options), "unseen": unseen}

    def ingest(self, client: FakeImap, **overrides) -> dict:
        options = {
            "account": "usuario",
            "mailbox": "INBOX",
            "limit": 50,
            "unseen": True,
        }
        options.update(overrides)
        return imap_reader.ingest_mailbox(self.store, client, **options)

    def cursor_file(self, mailbox: str = "INBOX") -> Path:
        return imap_reader.cursor_path(self.store, "usuario", mailbox)

    def triggers(self) -> list[dict]:
        return self.store.triggers()

    # ---- 1. bootstrap e incremental -----------------------------------
    def test_bootstrap_14_dias_unseen_e_incremental(self) -> None:
        client = FakeImap(messages=[self.message(1), self.message(2)])
        result = self.ingest(client)

        self.assertEqual(result["fetched"], 2)
        self.assertEqual(result["created"], 2)
        self.assertEqual(result["duplicates"], 0)
        self.assertEqual(result["errors"], 0)
        self.assertIs(result["shadow"], True)
        self.assertEqual(client.searches[0], ("SINCE", "18-Oct-2026"))
        self.assertEqual(client.searches[1], ("UNSEEN",))
        self.assertEqual(client.selected, [("INBOX", True)])

        cursor = json.loads(self.cursor_file().read_text(encoding="utf-8"))
        self.assertEqual(cursor["uidvalidity"], 123)
        self.assertEqual(cursor["last_uid"], 2)
        self.assertIs(cursor["bootstrap"]["done"], True)
        self.assertEqual(cursor["bootstrap"]["since"], "2026-10-18")

        result = self.ingest(client)
        self.assertEqual(result["fetched"], 0)
        self.assertEqual(result["created"], 0)
        self.assertEqual(result["duplicates"], 0)
        self.assertEqual(client.searches[-1], ("UID", "3:*"))
        self.assertEqual(len(self.triggers()), 2)

        self.ingest(client, mailbox="Archive", unseen=False)
        self.assertEqual(
            client.searches[-3:],
            [("UID", "3:*"), ("SINCE", "18-Oct-2026"), ("UID", "3:*")],
        )

    def test_incremental_nao_refaz_o_ultimo_uid(self) -> None:
        client = FakeImap(messages=[self.message(1), self.message(2)])
        self.ingest(client)
        self.assertEqual(
            client.fetch_args, [("1", "(RFC822)"), ("2", "(RFC822)")]
        )

        result = self.ingest(client)

        # O servidor responde `UID 3:*` com o último UID (2); o reader filtra.
        self.assertEqual(client.searches[-1], ("UID", "3:*"))
        self.assertEqual(result["fetched"], 0)
        self.assertEqual(result["errors"], 0)
        self.assertEqual(len(client.fetch_args), 2)
        self.assertEqual(
            json.loads(self.cursor_file().read_text(encoding="utf-8"))["last_uid"],
            2,
        )

    def test_search_uids_incremental_descarta_fronteira_do_cursor(self) -> None:
        class Stub:
            calls: list[tuple] = []

            def uid(self, command, *args):
                Stub.calls.append((command, *args))
                return ("OK", [b"2 3 4"])

        self.assertEqual(
            imap_reader.search_uids(Stub(), last_uid=2),
            [3, 4],
        )
        self.assertEqual(Stub.calls, [("SEARCH", "UID", "3:*")])

    # ---- 2. UIDVALIDITY novo ------------------------------------------
    def test_uidvalidity_novo_reseta_e_reprocessa(self) -> None:
        client = FakeImap(messages=[self.message(1), self.message(2)])
        first = self.ingest(client)
        self.assertEqual(first["created"], 2)

        client.uidvalidity = 999
        result = self.ingest(client)

        self.assertIn("uidvalidity_reset", result["warnings"])
        self.assertEqual(result["uidvalidity"], 999)
        self.assertEqual(result["fetched"], 2)
        self.assertEqual(result["created"], 0)
        self.assertEqual(result["duplicates"], 2)
        self.assertIn(("SINCE", "18-Oct-2026"), client.searches)

        cursor = json.loads(self.cursor_file().read_text(encoding="utf-8"))
        self.assertEqual(cursor["uidvalidity"], 999)
        self.assertEqual(cursor["last_uid"], 2)
        self.assertEqual(len(self.triggers()), 2)

    def test_get_uidvalidity_le_chave_untagged(self) -> None:
        client = FakeImap(uidvalidity=42)
        self.assertEqual(imap_reader.get_uidvalidity(client), 42)

        # `imaplib.IMAP4.response` devolve a própria chave untagged.
        missing = mock.Mock()
        missing.response.return_value = ("UIDVALIDITY", [None])
        with self.assertRaises(AtlasError) as ctx:
            imap_reader.get_uidvalidity(missing)
        self.assertNotIn("None", str(ctx.exception))

        for typ, data in (
            ("NO", [None]),
            ("BAD", [b"comando invalido"]),
            ("UIDVALIDITY", []),
        ):
            bad = mock.Mock()
            bad.response.return_value = (typ, data)
            with self.assertRaises(AtlasError):
                imap_reader.get_uidvalidity(bad)

        for value in (b"0", b"-1", b"abc"):
            bad = mock.Mock()
            bad.response.return_value = ("UIDVALIDITY", [value])
            with self.assertRaises(AtlasError):
                imap_reader.get_uidvalidity(bad)

    # ---- 3. cursor e falha de parse -----------------------------------
    def test_cursor_avanca_depois_do_add_e_para_no_erro(self) -> None:
        client = FakeImap(messages=[self.message(1)])
        self.ingest(client)
        cursor = json.loads(self.cursor_file().read_text(encoding="utf-8"))
        self.assertEqual(cursor["last_uid"], 1)

        client.messages = [self.message(2), self.message(3)]
        real_parse = imap_reader.parse_email

        def failing_parse(raw, **kwargs):
            if kwargs["uid"] == 3:
                raise ValueError("parse falhou")
            return real_parse(raw, **kwargs)

        with mock.patch("atlas_cli.imap_reader.parse_email", failing_parse):
            result = self.ingest(client)

        self.assertEqual(result["fetched"], 1)
        self.assertEqual(result["created"], 1)
        self.assertEqual(result["errors"], 1)
        self.assertEqual(result["consecutive_errors"], 1)
        self.assertIn("parse falhou", result["error"])
        self.assertEqual(result["last_uid"], 2)

        cursor = json.loads(self.cursor_file().read_text(encoding="utf-8"))
        self.assertEqual(cursor["last_uid"], 2)
        self.assertIn("parse falhou", cursor["last_error"])
        self.assertEqual(cursor["consecutive_errors"], 1)

    def test_falha_de_fetch_nao_avanca_cursor(self) -> None:
        client = FakeImap(messages=[self.message(1)])
        client.fail_fetch = {1}
        result = self.ingest(client)
        self.assertEqual(result["fetched"], 0)
        self.assertEqual(result["errors"], 1)
        cursor = json.loads(self.cursor_file().read_text(encoding="utf-8"))
        self.assertEqual(cursor["last_uid"], 0)
        self.assertIsNone(cursor["last_message_at"])

    # ---- 4. dry-run ----------------------------------------------------
    def test_dry_run_nao_cria_trigger_nem_cursor(self) -> None:
        client = FakeImap(messages=[self.message(1), self.message(2)])
        result = self.ingest(client, dry_run=True)

        self.assertEqual(result["fetched"], 2)
        self.assertEqual(result["created"], 2)
        self.assertEqual(self.triggers(), [])
        self.assertFalse(self.cursor_file().exists())
        # O dry-run não adquire o lock de mailbox: nenhum `.lock` em cursors.
        cursors = self.store.state_dir / "cursors"
        self.assertEqual(
            list(cursors.glob("*.lock")) if cursors.is_dir() else [],
            [],
            "dry-run criou lock de mailbox",
        )
        # Leitura atômica sem escrita: o diretório nem precisa existir.
        self.assertFalse(cursors.exists(), "dry-run criou state/cursors")

    # ---- 5. Gmail X-GM -------------------------------------------------
    def test_gmail_xgm_vence_message_id(self) -> None:
        message = self.message(1, message_id="<rfc@example.com>")
        message["x_gm_msgid"] = "111222333"
        message["x_gm_thrid"] = "444555666"
        client = FakeImap(
            messages=[message], capabilities=("IMAP4rev1", "X-GM-EXT-1")
        )

        self.ingest(client)

        rows = self.triggers()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["external_id"], "email:usuario:gmail:111222333")
        self.assertEqual(
            rows[0]["thread_key"], "email:usuario:gmail-thrid:444555666"
        )
        self.assertIn("X-GM-MSGID", client.fetch_args[0][1])

    def test_sem_capability_gmail_usa_message_id(self) -> None:
        message = self.message(1, message_id="<rfc@example.com>")
        message["x_gm_msgid"] = "111222333"
        client = FakeImap(messages=[message])

        self.ingest(client)

        rows = self.triggers()
        self.assertEqual(rows[0]["external_id"], "email:usuario:rfc:rfc@example.com")
        self.assertEqual(client.fetch_args[0][1], "(RFC822)")

    # ---- 6. idempotência por (source, external_id) ---------------------
    def test_mesma_mensagem_em_duas_mailboxes_nao_duplica_trigger(self) -> None:
        inbox = FakeImap(messages=[self.message(1, message_id="<same@example.com>")])
        archive = FakeImap(messages=[self.message(7, message_id="<same@example.com>")])

        first = self.ingest(inbox)
        second = self.ingest(archive, mailbox="Archive")

        self.assertEqual(first["created"], 1)
        self.assertEqual(second["created"], 0)
        self.assertEqual(second["duplicates"], 1)
        self.assertEqual(len(self.triggers()), 1)
        self.assertEqual(
            self.triggers()[0]["external_id"],
            "email:usuario:rfc:same@example.com",
        )

    # ---- 7. shadow report ----------------------------------------------
    def test_shadow_report_conta_e_grava_markdown_deterministico(self) -> None:
        client = FakeImap(
            messages=[
                self.message(1, subject="Fatura Nubank"),
                self.message(2, subject="Prazo SIGAA", sender="no-reply@sigaa.ufmg.br"),
                self.message(3, subject="Sem rota", sender="alguem@example.com"),
            ]
        )
        self.ingest(client)

        text = imap_reader.shadow_report(self.store)
        path = self.home / "reports" / "shadow" / f"{TODAY}.md"
        self.assertTrue(path.exists())
        self.assertEqual(path.read_text(encoding="utf-8"), text)
        self.assertEqual(text, imap_reader.shadow_report(self.store))

        self.assertIn(f"# SHADOW {TODAY}", text)
        self.assertIn("- mensagens lidas: 3", text)
        self.assertIn("regras: routed 2, ignored 0, candidate 1", text)
        self.assertIn("financas 1", text)
        self.assertIn("faculdade 1", text)
        self.assertIn("sem_rota 1", text)
        for trigger in self.triggers():
            self.assertEqual(trigger["state"], "new")
            self.assertIs(trigger["meta"]["shadow"], True)

    def test_shadow_report_conta_pela_data_de_leitura_nao_pelo_date(self) -> None:
        # Smoke real: `Date:` de 30/10, reader rodou em 01/11.
        client = FakeImap(
            messages=[
                self.message(
                    1,
                    subject="Fatura Nubank",
                    date="Fri, 30 Oct 2026 09:00:00 -0300",
                ),
                self.message(
                    2,
                    subject="Prazo SIGAA",
                    sender="no-reply@sigaa.ufmg.br",
                    date="Thu, 29 Oct 2026 09:00:00 -0300",
                ),
            ]
        )
        self.ingest(client)

        for trigger in self.triggers():
            self.assertEqual(trigger["created_at"][:10], TODAY)
            self.assertNotEqual(trigger["received_at"][:10], TODAY)

        text = imap_reader.shadow_report(self.store, date=TODAY)
        self.assertIn("- mensagens lidas: 2", text)
        self.assertIn("regras: routed 2, ignored 0, candidate 0", text)

        old = imap_reader.shadow_report(self.store, date="2026-10-30")
        self.assertIn("- mensagens lidas: 0", old)

    def test_shadow_report_ignora_trigger_lido_no_dia_anterior(self) -> None:
        yesterday = Store(home=self.home, now="2026-10-31T14:00:00-03:00")
        client = FakeImap(
            messages=[
                self.message(
                    1,
                    subject="Fatura Nubank",
                    date="Sat, 01 Nov 2026 12:00:00 -0300",
                )
            ]
        )
        imap_reader.ingest_mailbox(
            yesterday,
            client,
            account="usuario",
            mailbox="INBOX",
            limit=50,
            unseen=True,
        )
        trigger = self.triggers()[0]
        self.assertEqual(trigger["created_at"][:10], "2026-10-31")
        self.assertEqual(trigger["received_at"][:10], TODAY)

        today_store = Store(home=self.home, now=NOW)
        text = imap_reader.shadow_report(today_store, date=TODAY)
        self.assertIn("- mensagens lidas: 0", text)
        old = imap_reader.shadow_report(today_store, date="2026-10-31")
        self.assertIn("- mensagens lidas: 1", old)

    def test_shadow_report_sem_created_at_cai_no_received_at(self) -> None:
        client = FakeImap(messages=[self.message(1)])
        self.ingest(client)

        rows = [dict(row) for row in self.store.triggers()]
        for row in rows:
            row.pop("created_at", None)
        write_jsonl(self.store.triggers_path, rows)

        text = imap_reader.shadow_report(self.store, date=TODAY)
        self.assertIn("- mensagens lidas: 1", text)

    # ---- 8. limit ------------------------------------------------------
    def test_limit_respeitado_e_bootstrap_retoma(self) -> None:
        client = FakeImap(
            messages=[self.message(1), self.message(2), self.message(3)]
        )
        first = self.ingest(client, limit=2)
        self.assertEqual(first["fetched"], 2)
        self.assertEqual(first["last_uid"], 2)
        cursor = json.loads(self.cursor_file().read_text(encoding="utf-8"))
        self.assertIs(cursor["bootstrap"]["done"], False)

        second = self.ingest(client, limit=2)
        self.assertEqual(second["fetched"], 1)
        self.assertEqual(second["created"], 1)
        self.assertEqual(second["duplicates"], 0)
        self.assertEqual(second["last_uid"], 3)
        cursor = json.loads(self.cursor_file().read_text(encoding="utf-8"))
        self.assertIs(cursor["bootstrap"]["done"], True)
        self.assertEqual(len(self.triggers()), 3)

    # ---- 9. config -----------------------------------------------------
    def test_load_config_le_secrets_env(self) -> None:
        env_path = self.home / "secrets.env"
        env_path.write_text(
            "\n".join(
                [
                    "# config local",
                    "ATLAS_IMAP_HOST=imap.example.com",
                    "ATLAS_IMAP_PORT=1993",
                    "ATLAS_IMAP_USER=eu@example.com",
                    'ATLAS_IMAP_PASS="segredo com espaço"',
                    "ATLAS_IMAP_FOLDERS=INBOX,Archive",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        with mock.patch.dict(os.environ, {}, clear=True):
            config = imap_reader.load_config(env_path)
        self.assertEqual(config["host"], "imap.example.com")
        self.assertEqual(config["port"], 1993)
        self.assertEqual(config["user"], "eu@example.com")
        self.assertEqual(config["password"], "segredo com espaço")
        self.assertEqual(config["folder"], "INBOX")

    def test_load_config_env_vence_arquivo(self) -> None:
        env_path = self.home / "secrets.env"
        env_path.write_text(
            "ATLAS_IMAP_HOST=arquivo\nATLAS_IMAP_USER=u\nATLAS_IMAP_PASS=p\n",
            encoding="utf-8",
        )
        with mock.patch.dict(
            os.environ, {"ATLAS_IMAP_HOST": "ambiente"}, clear=True
        ):
            config = imap_reader.load_config(env_path)
        self.assertEqual(config["host"], "ambiente")
        self.assertEqual(config["port"], imap_reader.DEFAULT_PORT)

    def test_load_config_ausente_nao_vaza_senha(self) -> None:
        env_path = self.home / "secrets.env"
        env_path.write_text(
            "ATLAS_IMAP_HOST=imap.example.com\n"
            "ATLAS_IMAP_USER=eu@example.com\n"
            "ATLAS_IMAP_PASS=segredo-super-secreto\n",
            encoding="utf-8",
        )
        with mock.patch.dict(
            os.environ, {"ATLAS_IMAP_PORT": "993"}, clear=True
        ):
            env_path.unlink()
            with self.assertRaises(AtlasError) as ctx:
                imap_reader.load_config(env_path)
        message = str(ctx.exception)
        self.assertIn("ATLAS_IMAP_HOST", message)
        self.assertNotIn("segredo-super-secreto", message)

    def test_load_config_variavel_ausente(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(AtlasError) as ctx:
                imap_reader.load_config(self.home / "nao-existe.env")
        self.assertIn("ATLAS_IMAP_HOST", str(ctx.exception))
        self.assertIn("ATLAS_IMAP_USER", str(ctx.exception))
        self.assertIn("ATLAS_IMAP_PASS", str(ctx.exception))

    # ---- 10. cliente real ----------------------------------------------
    def test_real_client_read_only_e_logout(self) -> None:
        class FakeSSL:
            instances: list["FakeSSL"] = []

            def __init__(self, host, port, timeout=None, ssl_context=None):
                self.host = host
                self.port = port
                self.timeout = timeout
                self.ssl_context = ssl_context
                self.calls: list[tuple] = []
                FakeSSL.instances.append(self)

            def login(self, user, password):
                self.calls.append(("login", user))
                return ("OK", [b"ok"])

            def select(self, mailbox, readonly=False):
                self.calls.append(("select", mailbox, readonly))
                return ("OK", [b"1"])

            def response(self, name):
                return ("OK", [b"1"])

            def uid(self, command, *args):
                return ("OK", [b""])

            def capability(self):
                return ("OK", [b"IMAP4rev1"])

            def close(self):
                self.calls.append(("close",))

            def logout(self):
                self.calls.append(("logout",))
                return ("BYE", [b"bye"])

        config = {
            "host": "imap.example.com",
            "port": 993,
            "user": "eu@example.com",
            "password": "segredo",
            "folder": "INBOX",
        }
        with mock.patch("atlas_cli.imap_reader.imaplib.IMAP4_SSL", FakeSSL):
            with imap_reader.RealImapClient(config) as client:
                client.select("INBOX")
                self.assertNotIn("segredo", repr(vars(client)))

        conn = FakeSSL.instances[0]
        self.assertEqual(conn.host, "imap.example.com")
        self.assertEqual(conn.port, 993)
        self.assertIn(("select", "INBOX", True), conn.calls)
        self.assertEqual(conn.calls[0], ("login", "eu@example.com"))
        self.assertIn(("logout",), conn.calls)


class cursorTestCase(unittest.TestCase):
    def setUp(self) -> None:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.home = Path(tempfile.mkdtemp(prefix="atlas-m1b1b2-cur-", dir=TMP_ROOT))
        self.store = Store(home=self.home, now=NOW)
        self.store.init()

    def tearDown(self) -> None:
        shutil.rmtree(self.home, ignore_errors=True)

    def test_cursor_path_sanitiza_e_roundtrip(self) -> None:
        path = imap_reader.cursor_path(self.store, "zi/ne", "[Gmail]/All Mail")
        self.assertTrue(path.name.startswith("imap-zi_ne-_Gmail__All_Mail-"))
        self.assertRegex(path.name, r"-[0-9a-f]{10}\.json$")
        cursor = imap_reader.read_cursor(self.store, "zi/ne", "[Gmail]/All Mail")
        self.assertIs(cursor["bootstrap"]["done"], False)
        cursor["last_uid"] = 4
        imap_reader.replace_cursor_snapshot(self.store, cursor)
        again = imap_reader.read_cursor(self.store, "zi/ne", "[Gmail]/All Mail")
        self.assertEqual(again["last_uid"], 4)
        text = path.read_text(encoding="utf-8")
        self.assertIn('"last_uid": 4', text)

    def test_shadow_report_data_invalida(self) -> None:
        with self.assertRaises(AtlasError):
            imap_reader.shadow_report(self.store, date="2026-13-99")


if __name__ == "__main__":
    unittest.main()
