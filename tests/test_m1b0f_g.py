"""Testes adversariais do M1b.0g: IMAP-04, BACKUP-02-R1, STATE-02-R1.

Cada teste força a intercalação que o bug de `225de1f` permitia e que a
correção precisa impedir. Nenhum chama `mark_failure`/`rebuild_db` fora do
caminho público: o teste percorre `ingest_mailbox`/`Store.rebuild_db`.
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
TMP_ROOT = REPO / "tests" / ".tmp"
NOW = "2026-11-01T14:00:00-03:00"
ACCOUNT = "usuario"
MAILBOX = "INBOX"

sys.path.insert(0, str(REPO))

from atlas_cli import imap_reader  # noqa: E402
from atlas_cli import store as store_module  # noqa: E402
from atlas_cli.store import Store  # noqa: E402
from atlas_cli.util import AtlasError  # noqa: E402

# Compatibilidade com o commit `225de1f`: lá o publicador de snapshot se
# chamava `write_cursor`. `getattr` com fallback direto avaliaria o atributo
# antigo mesmo quando inexistente no módulo atual; a resolução em dois passos
# só toca `write_cursor` quando o nome novo não existe (código antigo).
fn = getattr(imap_reader, "replace_cursor_snapshot", None)
if fn is None:
    fn = imap_reader.write_cursor


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.home = Path(tempfile.mkdtemp(prefix="atlas-m1b0f-g-", dir=TMP_ROOT))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.store = Store(home=self.home, now=NOW)
        self.store.init()

    def cursor(self) -> dict:
        return imap_reader.read_cursor(self.store, ACCOUNT, MAILBOX)


class _EmptyClient:
    """Fake IMAP sem mensagens: todo FETCH vira UID expurgado."""

    def __init__(self, uidvalidity: int = 123) -> None:
        self._uidvalidity = uidvalidity

    def select(self, mailbox: str, readonly: bool = True):
        return ("OK", [b"1"])

    def response(self, name: str):
        if name == "UIDVALIDITY":
            return ("UIDVALIDITY", [str(self._uidvalidity).encode()])
        return ("OK", [None])

    def capability(self):
        return ("OK", [b"IMAP4rev1"])

    def uid(self, command: str, *args):
        if command.upper() == "FETCH":
            return ("OK", [None])
        return ("OK", [b""])


class _BarrierSelectClient(_EmptyClient):
    """SELECT que espera B publicar progresso (ou desiste após um teto).

    Em `225de1f` B roda durante este SELECT e sinaliza `b_done`; no topo B
    está serializado atrás do lock por mailbox e o teto evita deadlock.
    """

    def __init__(self, entered, b_done, *, ok: bool, timeout: float = 1.0) -> None:
        super().__init__()
        self._entered = entered
        self._b_done = b_done
        self._ok = ok
        self._timeout = timeout

    def select(self, mailbox: str, readonly: bool = True):
        self._entered.set()
        self._b_done.wait(self._timeout)
        return ("OK" if self._ok else "NO", [b"1"])


class IngestConcurrencyTest(_Base):
    """IMAP-04: o snapshot velho de A não pode apagar o progresso de B."""

    def seed_progress(self) -> None:
        # A lê este cursor antes do SELECT: last_uid=7, high_water=50.
        cursor = self.cursor()
        cursor["last_uid"] = 7
        cursor["bootstrap"].update(
            done=False,
            since="2026-10-18",
            unseen=True,
            high_water=50,
            pending=[41, 42, 43],
        )
        fn(self.store, cursor)

    def run_interleaved(self, *, a_ok: bool, a_limit: int) -> dict:
        self.seed_progress()
        a_in_select = threading.Event()
        b_done = threading.Event()
        errors: list[BaseException] = []

        def leitor_a() -> None:
            try:
                client = _BarrierSelectClient(a_in_select, b_done, ok=a_ok)
                try:
                    imap_reader.ingest_mailbox(
                        self.store,
                        client,
                        account=ACCOUNT,
                        mailbox=MAILBOX,
                        limit=a_limit,
                    )
                except AtlasError:
                    if a_ok:
                        raise
            except BaseException as exc:  # noqa: BLE001 - re-raise no join
                errors.append(exc)

        def leitor_b() -> None:
            try:
                imap_reader.ingest_mailbox(
                    self.store,
                    _EmptyClient(),
                    account=ACCOUNT,
                    mailbox=MAILBOX,
                    limit=50,
                )
            except BaseException as exc:  # noqa: BLE001 - re-raise no join
                errors.append(exc)
            finally:
                b_done.set()

        thread_a = threading.Thread(target=leitor_a, name="a")
        thread_b = threading.Thread(target=leitor_b, name="b")
        thread_a.start()
        self.assertTrue(a_in_select.wait(10), "A não chegou ao SELECT")
        thread_b.start()
        thread_a.join(30)
        thread_b.join(30)
        self.assertFalse(thread_a.is_alive(), "A travou")
        self.assertFalse(thread_b.is_alive(), "B travou")
        if errors:
            raise errors[0]
        return self.cursor()

    def test_falha_de_select_de_a_nao_apaga_progresso_de_b(self) -> None:
        cursor = self.run_interleaved(a_ok=False, a_limit=50)
        self.assertEqual(cursor["last_uid"], 50)
        self.assertEqual(cursor["bootstrap"]["high_water"], 50)
        self.assertEqual(cursor["bootstrap"]["pending"], [])
        self.assertTrue(cursor["bootstrap"]["done"])

    def test_sucesso_de_a_com_limite_menor_nao_regride_progresso(self) -> None:
        # A processa só 1 UID (limit=1) e publicaria last_uid=7/pending=[42,43];
        # B processa o restante e deve ser o estado final.
        cursor = self.run_interleaved(a_ok=True, a_limit=1)
        self.assertEqual(cursor["last_uid"], 50)
        self.assertEqual(cursor["bootstrap"]["high_water"], 50)
        self.assertEqual(cursor["bootstrap"]["pending"], [])
        self.assertTrue(cursor["bootstrap"]["done"])

    def test_replace_cursor_snapshot_bloqueia_atras_do_lock_de_mailbox(self) -> None:
        # IMAP-04-R4: `replace_cursor_snapshot` publica um snapshot completo e
        # precisa serializar com o ingest. Sem o lock de mailbox, B gravava
        # durante o SELECT de A e o `_write_progress` de A apagava o valor.
        self.seed_progress()
        a_in_select = threading.Event()
        release_a = threading.Event()
        b_entered = threading.Event()
        b_returned = threading.Event()
        a_result: dict = {}
        errors: list[BaseException] = []

        def leitor_a() -> None:
            try:
                client = _BarrierSelectClient(
                    a_in_select, release_a, ok=True, timeout=5.0
                )
                a_result["value"] = imap_reader.ingest_mailbox(
                    self.store, client, account=ACCOUNT, mailbox=MAILBOX, limit=50
                )
            except BaseException as exc:  # noqa: BLE001 - re-raise no join
                errors.append(exc)

        def escritor_b() -> None:
            try:
                snapshot = self.cursor()  # obsoleto: last_uid=7, done=False
                snapshot["last_uid"] = 99
                b_entered.set()
                fn(self.store, snapshot)
            except BaseException as exc:  # noqa: BLE001 - re-raise no join
                errors.append(exc)
            finally:
                b_returned.set()

        thread_a = threading.Thread(target=leitor_a, name="a")
        thread_b = threading.Thread(target=escritor_b, name="b")
        thread_a.start()
        self.assertTrue(a_in_select.wait(10), "A não chegou ao SELECT")
        thread_b.start()
        self.assertTrue(b_entered.wait(10), "B não iniciou")
        # B deve bloquear atrás do lock da mailbox que A segura no SELECT.
        self.assertFalse(
            b_returned.wait(1.0),
            "replace_cursor_snapshot não bloqueou atrás do lock da mailbox",
        )
        # Enquanto A processa, o snapshot de B não é visível.
        self.assertEqual(self.cursor()["last_uid"], 7)
        release_a.set()
        thread_a.join(30)
        thread_b.join(30)
        self.assertFalse(thread_a.is_alive(), "A travou")
        self.assertFalse(thread_b.is_alive(), "B travou")
        if errors:
            raise errors[0]
        self.assertTrue(b_returned.wait(5), "replace_cursor_snapshot não concluiu")
        # O progresso final do ingest de A não é o snapshot obsoleto de B.
        self.assertEqual(a_result["value"]["last_uid"], 50)
        self.assertTrue(a_result["value"]["uidvalidity"])
        # Serialização total: B só escreve depois de A liberar a mailbox, então
        # o snapshot de B é o último. O lock impede intercalação — não corrige
        # snapshot obsoleto; para progresso do ingest o caminho é `_write_progress`.
        self.assertEqual(self.cursor()["last_uid"], 99)

    def test_timeout_do_lock_de_mailbox_falha_sem_tocar_no_cursor(self) -> None:
        # IMAP-04-R2: o lock de mailbox tem timeout próprio; estourar falha
        # limpo, sem `mark_attempt`/progresso.
        self.seed_progress()
        holding = threading.Event()
        release = threading.Event()

        def segura_lock() -> None:
            with imap_reader._mailbox_lock(self.store, ACCOUNT, MAILBOX):
                holding.set()
                self.assertTrue(release.wait(10))

        holder = threading.Thread(target=segura_lock, name="holder")
        holder.start()
        try:
            self.assertTrue(holding.wait(10), "holder não pegou o lock")
            with mock.patch.object(
                imap_reader, "MAILBOX_LOCK_TIMEOUT_SECONDS", 0.1
            ):
                with self.assertRaises(AtlasError) as ctx:
                    imap_reader.ingest_mailbox(
                        self.store,
                        _EmptyClient(),
                        account=ACCOUNT,
                        mailbox=MAILBOX,
                    )
            self.assertIn("outro leitor", str(ctx.exception))
            cursor = self.cursor()
            self.assertEqual(cursor["last_uid"], 7)
            self.assertEqual(cursor["bootstrap"]["pending"], [41, 42, 43])
            self.assertIsNone(cursor["last_attempt_at"])
        finally:
            release.set()
            holder.join(30)
        self.assertFalse(holder.is_alive(), "holder travou")


class StateLockOwnerTest(_Base):
    """STATE-02-R1: `rebuild_db` só pula o flock se a thread dona o segura."""

    def seed_demand(self) -> None:
        with self.store.transaction("test.demand") as tx:
            tx.upsert_demand(
                {
                    "id": "dem-0",
                    "area": "projetos",
                    "state": "new",
                    "title": "Demanda 0",
                    "updated_at": NOW,
                    "due": None,
                }
            )

    def test_rebuild_db_espera_outra_thread_liberar_o_lock(self) -> None:
        self.seed_demand()
        a_holding = threading.Event()
        release_a = threading.Event()
        b_started = threading.Event()
        b_blocked = threading.Event()
        rebuild_threads: list[int] = []
        blocked_threads: list[int] = []
        errors: list[BaseException] = []
        original = Store._rebuild_db_unlocked
        real_flock = store_module.fcntl.flock

        def spy(store):
            rebuild_threads.append(threading.get_ident())
            return original(store)

        def spying_flock(fd, op):
            try:
                return real_flock(fd, op)
            except BlockingIOError:
                blocked_threads.append(threading.get_ident())
                b_blocked.set()
                raise

        def transacao_a() -> None:
            try:
                with self.store.transaction("test.hold") as tx:
                    tx.upsert_demand(
                        {
                            "id": "dem-hold",
                            "area": "projetos",
                            "state": "new",
                            "title": "Demanda retida",
                            "updated_at": NOW,
                            "due": None,
                        }
                    )
                    a_holding.set()
                    self.assertTrue(release_a.wait(10))
            except BaseException as exc:  # noqa: BLE001 - re-raise no join
                errors.append(exc)

        def rebuild_b() -> None:
            b_started.set()
            try:
                self.store.rebuild_db()
            except BaseException as exc:  # noqa: BLE001 - re-raise no join
                errors.append(exc)

        with mock.patch.object(Store, "_rebuild_db_unlocked", spy), mock.patch.object(
            store_module.fcntl, "flock", spying_flock
        ):
            thread_a = threading.Thread(target=transacao_a, name="a")
            thread_b = threading.Thread(target=rebuild_b, name="b")
            thread_a.start()
            self.assertTrue(a_holding.wait(10), "A não segurou o lock")
            thread_b.start()
            self.assertTrue(b_started.wait(10), "B não iniciou")
            # Prova causal: B tentou adquirir o flock e bloqueou enquanto A o
            # segurava. Em `225de1f` B pula o `flock` (depth > 0) e este evento
            # nunca dispara — o teste falha aqui por comportamento, não por
            # timeout.
            self.assertTrue(
                b_blocked.wait(2.0),
                "B não tentou adquirir o flock (rebuild_db pulou o lock)",
            )
            release_a.set()
            thread_a.join(30)
            thread_b.join(30)

        self.assertFalse(thread_a.is_alive(), "A travou")
        self.assertFalse(thread_b.is_alive(), "B travou")
        if errors:
            raise errors[0]

        # Quem bloqueou no flock foi a thread b (tentativa real de adquirir).
        self.assertIn(thread_b.ident, blocked_threads)
        # A reconstrução observada foi a de b, não a do commit de A.
        self.assertIn(thread_b.ident, rebuild_threads)
        self.assertNotIn(thread_a.ident, rebuild_threads)

        # O SQLite projetado precisa refletir os registros canônicos finais
        # (id/title/state), não apenas a presença dos IDs.
        canonical = {d["id"]: d for d in self.store.demands()}
        self.assertEqual(sorted(canonical), ["dem-0", "dem-hold"])
        conn = sqlite3.connect(self.store.db_path)
        try:
            conn.row_factory = sqlite3.Row
            rows = {
                row["id"]: row
                for row in conn.execute("SELECT id, title, state FROM demands")
            }
        finally:
            conn.close()
        self.assertEqual(sorted(rows), ["dem-0", "dem-hold"])
        for demand_id, demand in canonical.items():
            self.assertEqual(rows[demand_id]["title"], demand["title"])
            self.assertEqual(rows[demand_id]["state"], demand["state"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
