"""Testes do M1b.0f-B: identidade Gmail, bootstrap e liveness do reader."""

from __future__ import annotations

import imaplib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
TMP_ROOT = REPO / "tests" / ".tmp"
NOW = "2026-11-01T14:00:00-03:00"

sys.path.insert(0, str(REPO))

from atlas_cli import cli, control, health, imap_reader  # noqa: E402
from atlas_cli.email_parse import parse_email  # noqa: E402
from atlas_cli.store import Store  # noqa: E402
from atlas_cli.util import AtlasError  # noqa: E402

MESSAGE_ID = "<msg@example.com>"


def raw_email(
    *,
    extra_headers: tuple[bytes, ...] = (),
    body: bytes = b"corpo",
    message_id: str = MESSAGE_ID,
) -> bytes:
    lines = [
        b"From: no-reply@nubank.com.br",
        b"To: eu@example.com",
        b"Subject: Fatura",
        f"Message-ID: {message_id}".encode(),
        b"Date: Sat, 01 Nov 2026 12:00:00 -0300",
        *extra_headers,
        b"",
        body,
    ]
    return b"\r\n".join(lines)


class FakeImap:
    """Fake com semântica de SEARCH por critério (SINCE/UNSEEN/ALL).

    `uidnext` aceita um inteiro (valor fixo), `None` (servidor sem UIDNEXT,
    exercitando o fallback `max_uid`) ou `"auto"` (maior UID + 1, como um
    servidor real sem expurgo). `on_search`/`on_fetch` simulam mensagens
    que chegam no meio da rodada.
    """

    def __init__(
        self,
        *,
        uidvalidity: int = 123,
        capabilities=("IMAP4rev1",),
        messages=(),
        select_status: str = "OK",
        search_status: str = "OK",
        uidnext="auto",
        on_search=None,
        on_fetch=None,
    ) -> None:
        self.uidvalidity = uidvalidity
        self.capabilities_list = list(capabilities)
        self.messages = sorted(
            (dict(item) for item in messages), key=lambda item: item["uid"]
        )
        self.select_status = select_status
        self.search_status = search_status
        self.uidnext = uidnext
        self.on_search = on_search
        self.on_fetch = on_fetch
        self.selected: list[tuple] = []
        self.searches: list[tuple] = []
        self.fetch_args: list[tuple] = []
        self.fail_fetch: set[int] = set()

    def select(self, mailbox: str, readonly: bool = True):
        self.selected.append((mailbox, readonly))
        return (self.select_status, [b"1"])

    def response(self, name: str):
        if name == "UIDVALIDITY":
            return ("UIDVALIDITY", [str(self.uidvalidity).encode()])
        if name == "UIDNEXT":
            value = self.uidnext
            if value == "auto":
                value = self.messages[-1]["uid"] + 1 if self.messages else 1
            if value is None:
                return ("OK", [None])
            return ("UIDNEXT", [str(value).encode()])
        return ("OK", [None])

    def capability(self):
        return ("OK", [" ".join(self.capabilities_list).encode()])

    def uid(self, command: str, *args):
        command = command.upper()
        if command == "SEARCH":
            self.searches.append(tuple(str(arg) for arg in args))
            if self.search_status != "OK":
                return (self.search_status, [b""])
            if self.on_search is not None:
                self.on_search(args[0], args)
            criteria = args[0]
            if criteria == "SINCE":
                uids = [m["uid"] for m in self.messages if m.get("recent", True)]
            elif criteria == "UNSEEN":
                uids = [m["uid"] for m in self.messages if m.get("unseen", True)]
            elif criteria == "ALL":
                uids = [m["uid"] for m in self.messages]
            else:
                spec = str(args[1])
                all_uids = [m["uid"] for m in self.messages]
                if ":" not in spec:
                    exact = int(spec)
                    uids = [uid for uid in all_uids if uid == exact]
                else:
                    lower = int(spec.split(":")[0])
                    if all_uids and lower > all_uids[-1]:
                        uids = [all_uids[-1]]
                    else:
                        uids = [uid for uid in all_uids if uid >= lower]
            return ("OK", [" ".join(str(uid) for uid in uids).encode()])
        if command == "FETCH":
            self.fetch_args.append(tuple(str(arg) for arg in args))
            uid = int(args[0])
            if self.on_fetch is not None:
                self.on_fetch(uid)
            if uid in self.fail_fetch:
                return ("BAD", [b"fetch falhou"])
            message = next((m for m in self.messages if m["uid"] == uid), None)
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


def message(uid: int, **options) -> dict:
    unseen = options.pop("unseen", True)
    recent = options.pop("recent", True)
    body = options.pop("body", f"corpo {uid}".encode())
    extra = options.pop("extra_headers", ())
    return {
        "uid": uid,
        "raw": raw_email(
            body=body,
            extra_headers=extra,
            message_id=f"<msg-{uid}@example.com>",
        ),
        "unseen": unseen,
        "recent": recent,
    }


def parse(raw: bytes, **overrides):
    options = {
        "account": "usuario",
        "mailbox": "INBOX",
        "uidvalidity": 123,
        "uid": 1,
        "received_at": NOW,
    }
    options.update(overrides)
    return parse_email(raw, **options)


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.home = Path(tempfile.mkdtemp(prefix="atlas-m1b0f-b-", dir=TMP_ROOT))
        self.store = Store(home=self.home, now=NOW)
        self.store.init()

    def tearDown(self) -> None:
        shutil.rmtree(self.home, ignore_errors=True)

    def ingest(self, client, **overrides) -> dict:
        options = {
            "account": "usuario",
            "mailbox": "INBOX",
            "limit": 50,
            "unseen": True,
        }
        options.update(overrides)
        return imap_reader.ingest_mailbox(self.store, client, **options)

    def cursor(self, mailbox: str = "INBOX") -> dict:
        return imap_reader.read_cursor(self.store, "usuario", mailbox)

    def cursor_file(self, mailbox: str = "INBOX") -> Path:
        return imap_reader.cursor_path(self.store, "usuario", mailbox)

    def checks(self) -> dict[str, dict]:
        report = health.collect(self.store)
        return {check["name"]: check for check in report["checks"]}


# ---------------------------------------------------------------------------
# MAIL-01
# ---------------------------------------------------------------------------
class GmailHeaderTestCase(_Base):
    def test_fetch_vence_header_forjado(self) -> None:
        raw = raw_email(extra_headers=(b"X-GM-MSGID: 999", b"X-GM-THRID: 888"))
        injected = imap_reader._inject_gmail_headers(
            raw, {"x_gm_msgid": "111", "x_gm_thrid": "222"}
        )
        head = injected.split(b"\r\n\r\n", 1)[0]
        self.assertNotIn(b"999", head)
        self.assertNotIn(b"888", head)
        self.assertIn(b"X-GM-MSGID: 111", head)
        self.assertIn(b"X-GM-THRID: 222", head)

        parsed = parse(injected)
        self.assertEqual(parsed["trigger"]["external_id"], "email:usuario:gmail:111")
        self.assertEqual(
            parsed["trigger"]["thread_key"], "email:usuario:gmail-thrid:222"
        )

    def test_sem_fetch_descarta_forjado_e_usa_message_id(self) -> None:
        raw = raw_email(extra_headers=(b"X-GM-MSGID: 999",))
        injected = imap_reader._inject_gmail_headers(raw, {})
        self.assertNotIn(b"X-GM-MSGID", injected)
        parsed = parse(injected)
        self.assertEqual(
            parsed["trigger"]["external_id"], "email:usuario:rfc:msg@example.com"
        )

    def test_crlf_e_header_falso_no_corpo_nao_interferem(self) -> None:
        body = b"linha 1\r\n\r\nX-GM-MSGID: 666\r\nresto"
        raw = raw_email(body=body)
        injected = imap_reader._inject_gmail_headers(raw, {})
        self.assertTrue(injected.endswith(body))
        parsed = parse(injected)
        self.assertEqual(
            parsed["trigger"]["external_id"], "email:usuario:rfc:msg@example.com"
        )

    def test_header_duplicado_e_folded_e_removido(self) -> None:
        raw = raw_email(
            extra_headers=(
                b"X-GM-MSGID: 1",
                b"\t222",
                b"X-GM-MSGID: 333",
                b"X-GM-THRID: 444",
                b" Continuacao",
            )
        )
        stripped = imap_reader._strip_gmail_headers(raw)
        self.assertNotIn(b"X-GM", stripped)
        self.assertNotIn(b"222", stripped.split(b"\r\n\r\n", 1)[0])
        self.assertIn(b"Subject: Fatura", stripped)

        injected = imap_reader._inject_gmail_headers(raw, {"x_gm_msgid": "777"})
        head = injected.split(b"\r\n\r\n", 1)[0]
        self.assertEqual(head.count(b"X-GM-MSGID"), 1)
        self.assertIn(b"X-GM-MSGID: 777", head)
        self.assertNotIn(b"X-GM-THRID", head)

    def test_sem_separador_trata_tudo_como_header(self) -> None:
        raw = b"X-GM-MSGID: 5\r\nSubject: x"
        self.assertEqual(imap_reader._strip_gmail_headers(raw), b"Subject: x")

    def test_ingest_usa_xgm_do_fetch_contra_spoof(self) -> None:
        item = message(1, extra_headers=(b"X-GM-MSGID: 999",))
        item["x_gm_msgid"] = "111222"
        item["x_gm_thrid"] = "333444"
        client = FakeImap(
            messages=[item], capabilities=("IMAP4rev1", "X-GM-EXT-1")
        )
        self.ingest(client)
        rows = self.store.triggers()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["external_id"], "email:usuario:gmail:111222")


# ---------------------------------------------------------------------------
# IMAP-01
# ---------------------------------------------------------------------------
class BootstrapTestCase(_Base):
    def test_recente_lida_e_antiga_nao_lida_entram_na_uniao(self) -> None:
        client = FakeImap(
            messages=[
                message(1, recent=True, unseen=False),
                message(2, recent=False, unseen=True),
                message(3, recent=False, unseen=False),
            ]
        )
        result = self.ingest(client)

        self.assertEqual(result["fetched"], 2)
        self.assertEqual(
            sorted(row["external_id"] for row in self.store.triggers()),
            ["email:usuario:rfc:msg-1@example.com", "email:usuario:rfc:msg-2@example.com"],
        )
        self.assertEqual(client.searches[:2], [("SINCE", "18-Oct-2026"), ("UNSEEN",)])
        self.assertEqual(client.searches[2], ("UID", "4:*"))
        cursor = self.cursor()
        self.assertIs(cursor["bootstrap"]["done"], True)
        self.assertEqual(cursor["bootstrap"]["high_water"], 3)
        self.assertEqual(cursor["last_uid"], 3)

    def test_unseen_false_usa_so_since(self) -> None:
        client = FakeImap(
            messages=[message(1, recent=True, unseen=False), message(2, recent=False)]
        )
        result = self.ingest(client, unseen=False)
        self.assertEqual(result["fetched"], 1)
        self.assertNotIn(("UNSEEN",), client.searches)

    def test_selecao_vazia_marca_done_e_high_water(self) -> None:
        client = FakeImap(
            messages=[message(9, recent=False, unseen=False)]
        )
        result = self.ingest(client)
        self.assertEqual(result["fetched"], 0)
        cursor = self.cursor()
        self.assertIs(cursor["bootstrap"]["done"], True)
        self.assertEqual(cursor["bootstrap"]["pending"], [])
        self.assertEqual(cursor["bootstrap"]["high_water"], 9)
        self.assertEqual(cursor["last_uid"], 9)

    def test_paginacao_com_pending(self) -> None:
        client = FakeImap(
            messages=[message(1), message(2), message(3)]
        )
        first = self.ingest(client, limit=2)
        self.assertEqual(first["fetched"], 2)
        cursor = self.cursor()
        self.assertEqual(cursor["bootstrap"]["pending"], [3])
        self.assertIs(cursor["bootstrap"]["done"], False)
        self.assertEqual(cursor["last_uid"], 2)

        searches_before = len(client.searches)
        second = self.ingest(client, limit=2)
        self.assertEqual(second["fetched"], 1)
        self.assertEqual(client.searches[searches_before:], [("UID", "4:*")])
        cursor = self.cursor()
        self.assertEqual(cursor["bootstrap"]["pending"], [])
        self.assertIs(cursor["bootstrap"]["done"], True)
        self.assertEqual(cursor["last_uid"], 3)

    def test_mudanca_de_flag_nao_altera_selecao_em_andamento(self) -> None:
        client = FakeImap(
            messages=[
                message(1, recent=True, unseen=False),
                message(2, recent=False, unseen=True),
            ]
        )
        self.ingest(client, limit=1, unseen=True)
        self.assertEqual(self.cursor()["bootstrap"]["pending"], [2])

        result = self.ingest(client, unseen=False)
        self.assertEqual(result["fetched"], 1)
        self.assertIs(self.cursor()["bootstrap"]["done"], True)

    def test_uidvalidity_novo_reseta_bootstrap(self) -> None:
        client = FakeImap(messages=[message(1)])
        self.ingest(client)
        client.uidvalidity = 999
        client.messages = [message(5), message(6)]

        result = self.ingest(client)

        self.assertIn("uidvalidity_reset", result["warnings"])
        self.assertEqual(result["fetched"], 2)
        cursor = self.cursor()
        self.assertEqual(cursor["uidvalidity"], 999)
        self.assertIs(cursor["bootstrap"]["done"], True)
        self.assertEqual(cursor["last_uid"], 6)
        self.assertIn(("SINCE", "18-Oct-2026"), client.searches)

    def test_incremental_depois_de_done_e_high_water(self) -> None:
        client = FakeImap(messages=[message(4)])
        self.ingest(client)
        self.assertEqual(
            client.searches[-1], ("UID", "5:*")
        )

        client.messages.append(message(7))
        result = self.ingest(client)
        self.assertEqual(result["fetched"], 1)
        self.assertEqual(client.fetch_args[-1][0], "7")
        self.assertEqual(self.cursor()["last_uid"], 7)

        # `n:*` devolve o último UID mesmo abaixo de `n`: não refaz FETCH.
        client.messages.pop()
        calls = len(client.fetch_args)
        result = self.ingest(client)
        self.assertEqual(result["fetched"], 0)
        self.assertEqual(len(client.fetch_args), calls)

    def test_erro_em_search_registra_cursor(self) -> None:
        client = FakeImap(messages=[message(1)], search_status="NO")
        with self.assertRaises(AtlasError):
            self.ingest(client)
        cursor = self.cursor()
        self.assertEqual(cursor["last_attempt_at"], NOW)
        self.assertEqual(cursor["consecutive_errors"], 1)
        self.assertIn("busca IMAP falhou", cursor["last_error"])


class CursorTestCase(_Base):
    def test_nomes_distintos_para_identidades_colidentes_em_safe(self) -> None:
        first = imap_reader.cursor_path(self.store, "a/b", "INBOX")
        second = imap_reader.cursor_path(self.store, "a_b", "INBOX")
        self.assertNotEqual(first.name, second.name)

    def test_read_cursor_confere_identidade(self) -> None:
        cursor = imap_reader.empty_cursor("a/b", "INBOX")
        path = imap_reader.replace_cursor_snapshot(self.store, cursor)
        other = imap_reader.cursor_path(self.store, "a_b", "INBOX")
        other.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        with self.assertRaises(AtlasError) as ctx:
            imap_reader.read_cursor(self.store, "a_b", "INBOX")
        self.assertIn("outra mailbox", str(ctx.exception))

    def test_read_cursor_valida_pending(self) -> None:
        cursor = imap_reader.empty_cursor("usuario", "INBOX")
        path = imap_reader.replace_cursor_snapshot(self.store, cursor)
        broken = dict(cursor)
        broken["bootstrap"] = {
            "done": False,
            "since": None,
            "unseen": True,
            "high_water": None,
            "pending": ["3"],
        }
        path.write_text(json.dumps(broken), encoding="utf-8")
        with self.assertRaises(AtlasError):
            imap_reader.read_cursor(self.store, "usuario", "INBOX")


# ---------------------------------------------------------------------------
# HEALTH-01
# ---------------------------------------------------------------------------
class HealthTestCase(_Base):
    def test_poll_vazio_bem_sucedido_avanca_success(self) -> None:
        client = FakeImap()
        self.ingest(client)
        cursor = self.cursor()
        self.assertEqual(cursor["last_success_at"], NOW)
        self.assertIsNone(cursor["last_message_at"])
        self.assertEqual(self.checks()["cursors"]["severity"], "info")

    def test_falha_antes_do_loop_registra_attempt_e_erro(self) -> None:
        client = FakeImap(select_status="NO")
        with self.assertRaises(AtlasError):
            self.ingest(client)

        cursor = self.cursor()
        self.assertEqual(cursor["last_attempt_at"], NOW)
        self.assertIsNone(cursor["last_success_at"])
        self.assertIn("SELECT falhou", cursor["last_error"])
        check = self.checks()["cursors"]
        self.assertEqual(check["severity"], "warn")
        self.assertIn("sem sucesso desde", check["detail"])

    def test_stale_usa_last_success_nao_a_ultima_mensagem(self) -> None:
        cursor = imap_reader.empty_cursor("usuario", "INBOX")
        cursor["last_success_at"] = "2026-11-01T09:00:00-03:00"
        cursor["last_message_at"] = "2026-11-01T13:50:00-03:00"
        imap_reader.replace_cursor_snapshot(self.store, cursor)

        check = self.checks()["cursors"]
        self.assertEqual(check["severity"], "warn")
        self.assertIn("5.0h", check["detail"])

    def test_sem_sucesso_nem_attempt_e_sem_poll(self) -> None:
        imap_reader.replace_cursor_snapshot(
            self.store, imap_reader.empty_cursor("usuario", "INBOX")
        )
        check = self.checks()["cursors"]
        self.assertEqual(check["severity"], "warn")
        self.assertIn("sem poll", check["detail"])


# ---------------------------------------------------------------------------
# pausa na CLI
# ---------------------------------------------------------------------------
class IngestPauseTestCase(_Base):
    def test_pausado_nao_le_config_nem_abre_conexao(self) -> None:
        control.pause(self.store, "manutencao")
        args = SimpleNamespace(
            ingest_command="email",
            mailbox=None,
            account="usuario",
            limit=50,
            since=None,
            unseen=True,
            dry_run=False,
        )
        with mock.patch("atlas_cli.cli.imap_reader.load_config") as load_config:
            with mock.patch("atlas_cli.cli.imap_reader.RealImapClient") as real:
                with self.assertRaises(AtlasError) as ctx:
                    cli.cmd_ingest(self.store, args)
        self.assertIn("atlas pausado: reader", str(ctx.exception))
        load_config.assert_not_called()
        real.assert_not_called()


# ---------------------------------------------------------------------------
# M1b.0f-D1: IMAP-01 fronteira capturada antes das buscas
# ---------------------------------------------------------------------------
class BootstrapFrontierTestCase(_Base):
    def test_get_uidnext_aceita_untagged_e_ok(self) -> None:
        client = mock.Mock()
        client.response.return_value = ("UIDNEXT", [b"42"])
        self.assertEqual(imap_reader.get_uidnext(client), 42)
        client.response.return_value = ("OK", [b"7"])
        self.assertEqual(imap_reader.get_uidnext(client), 7)
        for typ, data in (
            ("NO", [b"1"]),
            ("OK", [None]),
            ("UIDNEXT", []),
            ("UIDNEXT", [b"abc"]),
            ("UIDNEXT", [b"0"]),
        ):
            client.response.return_value = (typ, data)
            self.assertIsNone(imap_reader.get_uidnext(client))

    def test_uid_que_chega_durante_as_buscas_fica_para_incremental(self) -> None:
        client = FakeImap(messages=[message(1), message(2)], uidnext=3)
        arrived: list[int] = []

        def arrive(criteria, args):
            if criteria == "SINCE" and not arrived:
                arrived.append(5)
                client.messages = sorted(
                    [*client.messages, message(5)], key=lambda item: item["uid"]
                )

        client.on_search = arrive
        result = self.ingest(client, limit=2)

        self.assertEqual(arrived, [5])
        self.assertEqual(result["fetched"], 2)
        cursor = self.cursor()
        self.assertEqual(cursor["bootstrap"]["high_water"], 2)
        self.assertEqual(cursor["bootstrap"]["pending"], [])
        self.assertIs(cursor["bootstrap"]["done"], True)
        self.assertEqual(cursor["last_uid"], 2)

        # A mensagem que chegou depois da fronteira entra na próxima rodada.
        result = self.ingest(client)
        self.assertEqual(result["fetched"], 1)
        self.assertEqual(self.cursor()["last_uid"], 5)

    def test_uid_que_chega_durante_o_fetch_fica_para_incremental(self) -> None:
        client = FakeImap(messages=[message(1), message(2)], uidnext=3)
        arrived: list[int] = []

        def arrive(uid):
            if not arrived:
                arrived.append(uid)
                client.messages = sorted(
                    [*client.messages, message(5)], key=lambda item: item["uid"]
                )

        client.on_fetch = arrive
        result = self.ingest(client, limit=2)

        self.assertEqual(arrived, [1])
        self.assertEqual(result["fetched"], 2)
        cursor = self.cursor()
        self.assertEqual(cursor["bootstrap"]["high_water"], 2)
        self.assertIs(cursor["bootstrap"]["done"], True)
        self.assertEqual(cursor["last_uid"], 2)
        self.assertNotIn(("UID", "3:*"), client.searches)

        result = self.ingest(client)
        self.assertEqual(result["fetched"], 1)
        self.assertEqual(self.cursor()["last_uid"], 5)

    def test_mailbox_vazia_fecha_bootstrap_com_high_water_zero(self) -> None:
        client = FakeImap(uidnext=1)
        result = self.ingest(client)

        self.assertEqual(result["fetched"], 0)
        cursor = self.cursor()
        self.assertEqual(cursor["bootstrap"]["high_water"], 0)
        self.assertEqual(cursor["bootstrap"]["pending"], [])
        self.assertIs(cursor["bootstrap"]["done"], True)
        self.assertEqual(cursor["last_uid"], 0)

    def test_sem_uidnext_usa_max_uid_antes_das_buscas(self) -> None:
        client = FakeImap(
            messages=[message(1), message(2), message(3)], uidnext=None
        )
        result = self.ingest(client)

        self.assertEqual(result["fetched"], 3)
        self.assertEqual(
            client.searches[:3],
            [("ALL",), ("SINCE", "18-Oct-2026"), ("UNSEEN",)],
        )
        cursor = self.cursor()
        self.assertEqual(cursor["bootstrap"]["high_water"], 3)
        self.assertEqual(cursor["last_uid"], 3)
        self.assertNotIn(("UID", "1:*"), client.searches)


# ---------------------------------------------------------------------------
# M1b.0f-D1: IMAP-02 UID expurgado entre a busca e o FETCH
# ---------------------------------------------------------------------------
class ExpungedUidTestCase(_Base):
    def test_uid_expurgado_sai_do_pending_e_proxima_rodada_ingere(self) -> None:
        client = FakeImap(messages=[message(2), message(3)], uidnext=4)
        first = self.ingest(client, limit=1)
        self.assertEqual(first["fetched"], 1)
        self.assertEqual(self.cursor()["bootstrap"]["pending"], [3])

        # UID 3 expurgado, UID 4 chegou.
        client.messages = [message(4)]
        result = self.ingest(client, limit=5)

        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["errors"], 0)
        self.assertEqual(result["fetched"], 1)
        cursor = self.cursor()
        self.assertIs(cursor["bootstrap"]["done"], True)
        self.assertEqual(cursor["last_uid"], 4)

    def test_falha_na_checagem_de_existencia_mantem_erro(self) -> None:
        client = FakeImap(messages=[message(2), message(3)], uidnext=4)
        self.ingest(client, limit=1)

        client.messages = []
        client.search_status = "NO"
        result = self.ingest(client)

        self.assertEqual(result["skipped"], 0)
        self.assertEqual(result["errors"], 1)
        self.assertIn("mensagem sem RFC822: uid 3", result["error"])
        cursor = self.cursor()
        self.assertEqual(cursor["bootstrap"]["pending"], [3])
        self.assertIs(cursor["bootstrap"]["done"], False)

    def test_fetch_bad_nao_e_descartado(self) -> None:
        client = FakeImap(messages=[message(1)])
        client.fail_fetch = {1}
        result = self.ingest(client)

        self.assertEqual(result["skipped"], 0)
        self.assertEqual(result["errors"], 1)
        self.assertEqual(self.cursor()["bootstrap"]["pending"], [1])


# ---------------------------------------------------------------------------
# M1b.0f-D1: IMAP-03 --limit como orçamento único da rodada
# ---------------------------------------------------------------------------
class LimitBudgetTestCase(_Base):
    def test_limit_unico_para_bootstrap_e_incremental(self) -> None:
        client = FakeImap(messages=[message(1)], uidnext=2)
        arrived: list[int] = []

        def arrive(uid):
            if not arrived:
                arrived.append(uid)
                client.messages.append(message(2))

        client.on_fetch = arrive
        result = self.ingest(client, limit=1)

        self.assertEqual(result["fetched"], 1)
        self.assertEqual(result["last_uid"], 1)
        self.assertNotIn(("UID", "2:*"), client.searches)
        self.assertIs(self.cursor()["bootstrap"]["done"], True)

        result = self.ingest(client)
        self.assertEqual(result["fetched"], 1)
        self.assertEqual(self.cursor()["last_uid"], 2)


# ---------------------------------------------------------------------------
# M1b.0f-D1: MAIL-02 corpo byte-idêntico
# ---------------------------------------------------------------------------
class MailBodyBytesTestCase(_Base):
    def test_corpo_byte_identico_ao_remover_xgm(self) -> None:
        body = b"linha 1\r\n\r\nX-GM-MSGID: 666\r\nresto\r\n"
        raw = raw_email(
            extra_headers=(b"X-GM-MSGID: 9", b"X-GM-THRID: 8"), body=body
        )
        stripped = imap_reader._strip_gmail_headers(raw)

        head, separator, remainder = stripped.partition(b"\r\n\r\n")
        self.assertNotIn(b"X-GM", head)
        self.assertEqual(separator, b"\r\n\r\n")
        self.assertEqual(remainder, body)
        self.assertIn(b"Subject: Fatura", head)

    def test_corpo_message_rfc822_identico(self) -> None:
        inner = raw_email(message_id="<inner@example.com>", body=b"interno")
        raw = raw_email(
            extra_headers=(b"Content-Type: message/rfc822", b"X-GM-MSGID: 1"),
            body=inner,
        )
        stripped = imap_reader._strip_gmail_headers(raw)
        self.assertTrue(stripped.endswith(inner))
        self.assertEqual(stripped.partition(b"\r\n\r\n")[2], inner)

    def test_sem_xgm_preserva_bytes_e_content_hash(self) -> None:
        raw = raw_email(body=b"corpo\r\n\r\ncom linha em branco\r\n")
        self.assertEqual(imap_reader._strip_gmail_headers(raw), raw)

        injected = imap_reader._inject_gmail_headers(raw, {})
        self.assertEqual(injected, raw)
        first = parse(raw)["trigger"]["meta"]["content_hash"]
        second = parse(injected)["trigger"]["meta"]["content_hash"]
        self.assertEqual(first, second)


# ---------------------------------------------------------------------------
# M1b.0f-D1: HEALTH-01 tentativa e transporte
# ---------------------------------------------------------------------------
class TransportAttemptTestCase(_Base):
    def _config(self) -> dict:
        return {
            "host": "imap.example.com",
            "port": 993,
            "user": "eu@example.com",
            "password": "segredo",
            "folder": "INBOX",
        }

    def test_cliente_real_embrulha_erros_de_transporte(self) -> None:
        class Boom:
            def __init__(self, *args, **kwargs):
                pass

            def login(self, user, password):
                return ("OK", [b"ok"])

            def select(self, mailbox, readonly=False):
                raise imaplib.IMAP4.error("select boom")

            def response(self, name):
                raise OSError("response boom")

            def uid(self, command, *args):
                raise OSError("uid boom")

            def capability(self):
                raise imaplib.IMAP4.error("capability boom")

            def logout(self):
                pass

        with mock.patch("atlas_cli.imap_reader.imaplib.IMAP4_SSL", Boom):
            client = imap_reader.RealImapClient(self._config())

        for call, expected in (
            (lambda: client.select("INBOX"), "SELECT INBOX"),
            (lambda: client.response("UIDVALIDITY"), "consulta UIDVALIDITY"),
            (lambda: client.uid("SEARCH", "ALL"), "UID SEARCH"),
            (lambda: client.capability(), "CAPABILITY"),
        ):
            with self.assertRaises(AtlasError) as ctx:
                call()
            self.assertIn(expected, str(ctx.exception))

        class UidBoom:
            def uid(self, command, *args):
                raise OSError("uid boom")

        with self.assertRaises(AtlasError) as ctx:
            imap_reader.fetch_message(UidBoom(), 1, gmail=False)
        self.assertIn("FETCH uid 1", str(ctx.exception))

    def test_login_com_oserror_vira_atlaserror(self) -> None:
        class Boom:
            def __init__(self, *args, **kwargs):
                pass

            def login(self, user, password):
                raise OSError("sem rota")

            def shutdown(self):
                pass

        with mock.patch("atlas_cli.imap_reader.imaplib.IMAP4_SSL", Boom):
            with self.assertRaises(AtlasError) as ctx:
                imap_reader.RealImapClient(self._config())
        self.assertIn("falha no login IMAP", str(ctx.exception))
        self.assertNotIn("segredo", str(ctx.exception))

    def test_falha_de_conexao_na_cli_registra_attempt_e_erro(self) -> None:
        args = SimpleNamespace(
            ingest_command="email",
            mailbox=None,
            account="usuario",
            limit=50,
            since=None,
            unseen=True,
            dry_run=False,
        )
        boom = AtlasError("falha ao conectar no IMAP: timeout")
        with mock.patch(
            "atlas_cli.cli.imap_reader.load_config", return_value=self._config()
        ):
            with mock.patch(
                "atlas_cli.cli.imap_reader.RealImapClient", side_effect=boom
            ):
                with self.assertRaises(AtlasError):
                    cli.cmd_ingest(self.store, args)

        cursor = self.cursor()
        self.assertEqual(cursor["last_attempt_at"], NOW)
        self.assertEqual(cursor["consecutive_errors"], 1)
        self.assertIn("falha ao conectar", cursor["last_error"])
        self.assertIsNone(cursor["last_success_at"])
        check = self.checks()["cursors"]
        self.assertEqual(check["severity"], "warn")
        self.assertIn("sem sucesso desde", check["detail"])

    def test_tentativa_mais_nova_que_sucesso_gera_warn(self) -> None:
        cursor = imap_reader.empty_cursor("usuario", "INBOX")
        cursor["last_success_at"] = "2026-11-01T09:00:00-03:00"
        cursor["last_attempt_at"] = "2026-11-01T13:00:00-03:00"
        imap_reader.replace_cursor_snapshot(self.store, cursor)

        check = self.checks()["cursors"]
        self.assertEqual(check["severity"], "warn")
        self.assertIn("tentativa sem sucesso", check["detail"])


# ---------------------------------------------------------------------------
# M1b.0f-D1: HEALTH-02 sucesso (inclusive poll vazio) limpa o erro
# ---------------------------------------------------------------------------
class SuccessClearsErrorTestCase(_Base):
    def test_poll_vazio_limpa_erro_e_health_volta_a_info(self) -> None:
        failing = FakeImap(messages=[message(1)], select_status="NO")
        with self.assertRaises(AtlasError):
            self.ingest(failing)
        self.assertEqual(self.cursor()["consecutive_errors"], 1)
        self.assertEqual(self.checks()["cursors"]["severity"], "warn")

        for _ in range(2):
            result = self.ingest(FakeImap())
            self.assertEqual(result["fetched"], 0)
            self.assertIsNone(result["error"])
            cursor = self.cursor()
            self.assertEqual(cursor["last_success_at"], NOW)
            self.assertIsNone(cursor["last_error"])
            self.assertEqual(cursor["consecutive_errors"], 0)
            self.assertEqual(self.checks()["cursors"]["severity"], "info")


if __name__ == "__main__":
    unittest.main()
