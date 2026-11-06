"""Reader IMAP read-only + cursor por mailbox + relatório de shadow (M1b.1b2).

O reader nunca escreve no servidor: apenas `SELECT ... (READONLY)`,
`UID SEARCH`, `UID FETCH` e `CAPABILITY`. Triggers criados aqui carregam
`meta.shadow=true`; nenhuma demanda, notificação ou chamada de modelo
acontece nesta camada.
"""

from __future__ import annotations

import fcntl
import hashlib
import imaplib
import json
import os
import re
import ssl
import time
from contextlib import contextmanager
from datetime import date as _date
from datetime import timedelta
from pathlib import Path
from typing import Protocol

from . import control, ingest, triggers
from .email_parse import parse_email
from .store import Store
from .util import AtlasError, parse_dt, read_env_file, write_text

DEFAULT_ENV_PATH = Path("~/.config/atlas/secrets.env")
DEFAULT_PORT = 993
DEFAULT_FOLDER = "INBOX"
DEFAULT_BOOTSTRAP_DAYS = 14
FETCH_TIMEOUT_SECONDS = 30
#: Lock por `(account, mailbox)` cobre a rede de um poll inteiro (bootstrap
#: grande/servidor lento). O flock global continua em `LOCK_TIMEOUT_SECONDS`
#: (escritas curtas). Estourar aqui falha limpo, sem tocar no cursor.
MAILBOX_LOCK_TIMEOUT_SECONDS = 600.0

ENV_KEYS = (
    "ATLAS_IMAP_HOST",
    "ATLAS_IMAP_PORT",
    "ATLAS_IMAP_USER",
    "ATLAS_IMAP_PASS",
    "ATLAS_IMAP_FOLDERS",
)

#: Chaves cujo valor nunca aparece em mensagem de erro.
_SECRET_ENV = frozenset({"ATLAS_IMAP_PASS"})

_MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)

_SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]")
_X_GM_MSGID_RE = re.compile(rb"X-GM-MSGID\s+(\d+)")
_X_GM_THRID_RE = re.compile(rb"X-GM-THRID\s+(\d+)")


# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------
def _parse_port(raw: str) -> int:
    try:
        port = int(raw)
    except (TypeError, ValueError):
        raise AtlasError("ATLAS_IMAP_PORT inválido: esperado inteiro") from None
    if not 1 <= port <= 65535:
        raise AtlasError("ATLAS_IMAP_PORT fora da faixa 1..65535")
    return port


def load_config(env_path: Path | None = None) -> dict:
    """Lê `secrets.env` e as variáveis `ATLAS_IMAP_*` (env vence o arquivo).

    Nunca imprime nem inclui `password` em mensagem de erro.
    """
    path = Path(env_path).expanduser() if env_path else DEFAULT_ENV_PATH.expanduser()
    values = read_env_file(path)
    for key in ENV_KEYS:
        if key in os.environ:
            values[key] = os.environ[key]

    missing = [
        key
        for key in ("ATLAS_IMAP_HOST", "ATLAS_IMAP_USER", "ATLAS_IMAP_PASS")
        if not (values.get(key) or "").strip()
    ]
    if missing:
        raise AtlasError(
            "config IMAP incompleta (defina em "
            f"{path} ou no ambiente): " + ", ".join(missing)
        )

    raw_port = (values.get("ATLAS_IMAP_PORT") or "").strip()
    folders = (values.get("ATLAS_IMAP_FOLDERS") or "").strip()
    folder = folders.split(",")[0].strip() if folders else DEFAULT_FOLDER
    return {
        "host": values["ATLAS_IMAP_HOST"].strip(),
        "port": _parse_port(raw_port) if raw_port else DEFAULT_PORT,
        "user": values["ATLAS_IMAP_USER"].strip(),
        "password": values["ATLAS_IMAP_PASS"],
        "folder": folder or DEFAULT_FOLDER,
    }


# ---------------------------------------------------------------------------
# Cliente
# ---------------------------------------------------------------------------
class ImapClient(Protocol):
    """Contrato mínimo consumido pelo reader; testes usam um fake."""

    def select(self, mailbox: str, readonly: bool = True): ...

    def response(self, name: str): ...

    def uid(self, command: str, *args): ...

    def capability(self): ...


class RealImapClient:
    """Embrulho de `imaplib.IMAP4_SSL` limitado a leitura."""

    def __init__(self, config: dict, *, timeout: int = FETCH_TIMEOUT_SECONDS) -> None:
        # `create_default_context()` verifica hostname e cadeia (CERT_REQUIRED).
        # Nunca usar CERT_NONE: senão o login manda a senha em claro para um
        # servidor qualquer que responda na porta.
        try:
            conn = imaplib.IMAP4_SSL(
                config["host"],
                int(config["port"]),
                timeout=timeout,
                ssl_context=ssl.create_default_context(),
            )
        except (imaplib.IMAP4.error, OSError, KeyError, TypeError, ValueError) as exc:
            raise AtlasError(f"falha ao conectar no IMAP: {exc}") from exc
        try:
            conn.login(config["user"], config["password"])
        except (imaplib.IMAP4.error, OSError) as exc:
            try:
                conn.shutdown()
            except OSError:
                pass
            raise AtlasError(f"falha no login IMAP: {exc}") from exc
        self._conn = conn

    def select(self, mailbox: str, readonly: bool = True):
        try:
            return self._conn.select(mailbox, readonly=readonly)
        except (imaplib.IMAP4.error, OSError) as exc:
            raise AtlasError(f"SELECT {mailbox} falhou: {exc}") from exc

    def response(self, name: str):
        try:
            return self._conn.response(name)
        except (imaplib.IMAP4.error, OSError) as exc:
            raise AtlasError(f"consulta {name} falhou: {exc}") from exc

    def uid(self, command: str, *args):
        try:
            return self._conn.uid(command, *args)
        except (imaplib.IMAP4.error, OSError) as exc:
            raise AtlasError(f"UID {command} falhou: {exc}") from exc

    def capability(self):
        try:
            return self._conn.capability()
        except (imaplib.IMAP4.error, OSError) as exc:
            raise AtlasError(f"CAPABILITY falhou: {exc}") from exc

    def close(self):
        return self._conn.close()

    def logout(self):
        return self._conn.logout()

    def __enter__(self) -> "RealImapClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            self._conn.logout()
        except (imaplib.IMAP4.error, OSError):
            pass
        return False


# ---------------------------------------------------------------------------
# Cursor
# ---------------------------------------------------------------------------
def _safe(value: str) -> str:
    return _SAFE_RE.sub("_", value)


def _check_identity(account: str, mailbox: str) -> None:
    if not isinstance(account, str) or not account.strip():
        raise AtlasError("account vazio")
    if not isinstance(mailbox, str) or not mailbox.strip():
        raise AtlasError("mailbox vazia")


def _cursor_digest(account: str, mailbox: str) -> str:
    """Hash estável de `(account, mailbox)`; desempata `_safe` colidente."""
    material = f"{account}\x00{mailbox}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:10]


def cursor_path(store: Store, account: str, mailbox: str) -> Path:
    _check_identity(account, mailbox)
    safe = _safe(f"{account}-{mailbox}")
    name = f"imap-{safe}-{_cursor_digest(account, mailbox)}.json"
    return store.state_dir / "cursors" / name


def empty_cursor(account: str, mailbox: str) -> dict:
    return {
        "account": account,
        "mailbox": mailbox,
        "uidvalidity": None,
        "last_uid": 0,
        # `last_seen_at` legado continua aceito na leitura (ver health.py).
        "last_message_at": None,
        "last_success_at": None,
        "last_attempt_at": None,
        "bootstrap": {
            "done": False,
            "since": None,
            "unseen": True,
            "high_water": None,
            "pending": None,
        },
        "last_error": None,
        "consecutive_errors": 0,
    }


def _check_bootstrap(value, path: Path) -> dict:
    if not isinstance(value, dict):
        raise AtlasError(f"cursor inválido: bootstrap em {path}")
    merged = {
        "done": False,
        "since": None,
        "unseen": True,
        "high_water": None,
        "pending": None,
    }
    merged.update(value)
    if not isinstance(merged["done"], bool):
        raise AtlasError(f"cursor inválido: bootstrap.done em {path}")
    if not isinstance(merged["unseen"], bool):
        raise AtlasError(f"cursor inválido: bootstrap.unseen em {path}")
    for key in ("since",):
        if merged[key] is not None and not isinstance(merged[key], str):
            raise AtlasError(f"cursor inválido: bootstrap.{key} em {path}")
    high_water = merged["high_water"]
    if high_water is not None and (
        isinstance(high_water, bool)
        or not isinstance(high_water, int)
        or high_water < 0
    ):
        raise AtlasError(f"cursor inválido: bootstrap.high_water em {path}")
    pending = merged["pending"]
    if pending is not None:
        if not isinstance(pending, list) or any(
            isinstance(uid, bool) or not isinstance(uid, int) or uid <= 0
            for uid in pending
        ):
            raise AtlasError(f"cursor inválido: bootstrap.pending em {path}")
    return merged


def read_cursor(store: Store, account: str, mailbox: str) -> dict:
    path = cursor_path(store, account, mailbox)
    if not path.exists():
        return empty_cursor(account, mailbox)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AtlasError(f"cursor inválido: {path}") from exc
    if not isinstance(data, dict):
        raise AtlasError(f"cursor inválido: {path}")
    if data.get("account") != account or data.get("mailbox") != mailbox:
        raise AtlasError(
            f"cursor de outra mailbox em {path}: "
            f"{data.get('account')!r}/{data.get('mailbox')!r}"
        )
    cursor = empty_cursor(account, mailbox)
    cursor.update(data)
    cursor["bootstrap"] = _check_bootstrap(cursor.get("bootstrap"), path)
    last_uid = cursor.get("last_uid")
    if isinstance(last_uid, bool) or not isinstance(last_uid, int) or last_uid < 0:
        raise AtlasError(f"cursor inválido: last_uid em {path}")
    return cursor


def _write_cursor_unlocked(store: Store, cursor: dict) -> Path:
    path = cursor_path(store, cursor["account"], cursor["mailbox"])
    text = (
        json.dumps(cursor, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    write_text(path, text)
    return path


def replace_cursor_snapshot(store: Store, cursor: dict) -> Path:
    """Substitui o cursor inteiro por `cursor` (destrutivo).

    Destina-se exclusivamente a seed/teste/reset com a mailbox **parada**:
    publica o argumento como está, sem reler o estado atual sob o lock. Não é
    read-modify-write concorrente — a leitura que produziu `cursor` pode ser
    antiga e sobrescrever progresso alheio. O ingest publica progresso por
    `_write_progress`; este helper nunca deve ser usado no caminho de produção.

    Exige o lock por `(account, mailbox)` antes do `flock` global (ordem
    mailbox → global): um caller que publique um snapshot completo não pode
    intercalar com `ingest_mailbox` e recriar o IMAP-04.
    """
    with _mailbox_lock(store, cursor["account"], cursor["mailbox"]):
        with store.lock():
            return _write_cursor_unlocked(store, cursor)


def mark_attempt(
    store: Store, account: str, mailbox: str, *, now: str | None = None
) -> dict:
    """Registra que houve tentativa de poll antes de abrir a conexão.

    Relê o cursor sob o lock e altera só o campo de telemetria: um snapshot
    capturado antes do connect pode estar obsoleto e não pode sobrescrever
    `pending`/`high_water` que outro leitor acabou de gravar.
    """
    with store.lock():
        cursor = read_cursor(store, account, mailbox)
        cursor["last_attempt_at"] = now or store.now_iso
        _write_cursor_unlocked(store, cursor)
    return cursor


def mark_failure(
    store: Store,
    account: str,
    mailbox: str,
    exc: Exception,
    *,
    now: str | None = None,
) -> dict:
    """Registra falha de conexão/login sem apagar o progresso anterior."""
    with store.lock():
        cursor = read_cursor(store, account, mailbox)
        cursor["last_attempt_at"] = now or store.now_iso
        _record_failure(cursor, exc)
        _write_cursor_unlocked(store, cursor)
    return cursor


#: Campos de progresso publicados por `ingest_mailbox`. Telemetria
#: (`last_attempt_at`, `last_error`, `consecutive_errors`) pertence a
#: `mark_attempt`/`mark_failure` e nunca é sobrescrita por um snapshot de
#: progresso capturado antes do SELECT.
_PROGRESS_FIELDS = (
    "uidvalidity",
    "last_uid",
    "last_message_at",
    "bootstrap",
    "last_success_at",
)


def _mailbox_lock_path(store: Store, account: str, mailbox: str) -> Path:
    path = cursor_path(store, account, mailbox)
    return path.with_name("." + path.name + ".lock")


@contextmanager
def _mailbox_lock(store: Store, account: str, mailbox: str):
    """Exclusão por `(account, mailbox)`, separada do `flock` global.

    Dois leitores da mesma mailbox serializam: o segundo só lê o cursor
    depois que o primeiro publica o progresso. O `flock` global continua
    reservado a publicações curtas (`mark_attempt`/`mark_failure`/
    `_write_progress`), nunca à rede. Ordem de aquisição: mailbox → global.
    """
    lock_path = _mailbox_lock_path(store, account, mailbox)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    deadline = time.monotonic() + MAILBOX_LOCK_TIMEOUT_SECONDS
    acquired = False
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise AtlasError(
                        f"não foi possível obter {lock_path} em "
                        f"{MAILBOX_LOCK_TIMEOUT_SECONDS:g}s "
                        "(outro leitor processa a mailbox)"
                    )
                time.sleep(0.01)
        acquired = True
        yield
    finally:
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _write_progress(
    store: Store, cursor: dict, *, clear_error: bool = False
) -> None:
    """Publica só os campos de progresso, relendo o cursor sob o lock global.

    Um snapshot de progresso lido antes do SELECT não pode apagar o que outro
    leitor gravou: só `_PROGRESS_FIELDS` é copiado. No sucesso, `clear_error`
    limpa `last_error`/`consecutive_errors`; a falha passa por `mark_failure`.
    """
    with store.lock():
        current = read_cursor(store, cursor["account"], cursor["mailbox"])
        for key in _PROGRESS_FIELDS:
            current[key] = cursor[key]
        if clear_error:
            current["last_error"] = None
            current["consecutive_errors"] = 0
        _write_cursor_unlocked(store, current)


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------
def get_uidvalidity(client: ImapClient) -> int:
    """UIDVALIDITY da mailbox selecionada, direto do untagged do `SELECT`.

    `imaplib.IMAP4.response("UIDVALIDITY")` não emite comando: devolve a
    chave untagged com que o servidor respondeu ao `SELECT`, ou seja
    `("UIDVALIDITY", [b"123"])` — nunca `("OK", ...)`, que é o tipo do
    comando e não do código. `("UIDVALIDITY", [None])` é a ausência.
    """
    typ, data = client.response("UIDVALIDITY")
    typ = str(typ).upper()
    if typ not in ("UIDVALIDITY", "OK") or not data:
        raise AtlasError("UIDVALIDITY indisponível após select")
    value = data[0]
    if value is None:
        raise AtlasError("UIDVALIDITY indisponível após select")
    if isinstance(value, bytes):
        value = value.decode("ascii", "ignore")
    try:
        uidvalidity = int(str(value).strip())
    except ValueError:
        raise AtlasError(f"UIDVALIDITY inválido: {value!r}") from None
    if uidvalidity <= 0:
        raise AtlasError(f"UIDVALIDITY inválido: {value!r}")
    return uidvalidity


def get_uidnext(client: ImapClient) -> int | None:
    """UIDNEXT untagged da mailbox selecionada; `None` se ausente/inválido.

    Precisa ser lido antes de qualquer `SINCE`/`UNSEEN`: depois das buscas
    o valor pode já incluir um UID que chegou no meio do bootstrap.
    """
    try:
        typ, data = client.response("UIDNEXT")
    except AtlasError:
        return None
    typ = str(typ).upper()
    if typ not in ("UIDNEXT", "OK") or not data:
        return None
    value = data[0]
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("ascii", "ignore")
    try:
        uidnext = int(str(value).strip())
    except ValueError:
        return None
    return uidnext if uidnext > 0 else None


def _imap_date(value) -> str:
    if isinstance(value, _date):
        day = value
    else:
        text = str(value).strip()
        try:
            day = _date.fromisoformat(text)
        except ValueError:
            try:
                day = parse_dt(text).date()
            except AtlasError:
                raise AtlasError(f"data de busca inválida: {text!r}") from None
    return f"{day.day:02d}-{_MONTHS[day.month - 1]}-{day.year}"


def _parse_search(data) -> list[int]:
    uids: set[int] = set()
    for chunk in data or []:
        if chunk is None:
            continue
        if isinstance(chunk, bytes):
            chunk = chunk.decode("ascii", "ignore")
        for token in str(chunk).split():
            if token.isdigit():
                uids.add(int(token))
    return sorted(uids)


def search_uids(client: ImapClient, *, last_uid: int) -> list[int]:
    """UIDs ordenados, sem duplicatas, sempre além do cursor."""
    typ, data = client.uid("SEARCH", "UID", f"{int(last_uid) + 1}:*")
    if typ != "OK":
        raise AtlasError(f"busca IMAP falhou: {typ}")
    # `UID n:*` devolve o último UID mesmo quando ele é menor que `n`
    # (RFC 3501); filtrar evita reprocessar mensagens já commitadas.
    return [uid for uid in _parse_search(data) if uid > int(last_uid)]


def bootstrap_uids(
    client: ImapClient, since, unseen: bool, *, cap: int | None = None
) -> list[int]:
    """União `SINCE` ∪ `UNSEEN`: recentes ou ainda não lidas.

    Não é interseção: uma mensagem antiga e não lida precisa entrar no
    bootstrap mesmo fora da janela de dias. `cap` descarta UIDs acima da
    fronteira capturada antes da busca (chegaram durante o bootstrap).
    """
    if since is None:
        raise AtlasError("bootstrap sem data de início")
    typ, data = client.uid("SEARCH", "SINCE", _imap_date(since))
    if typ != "OK":
        raise AtlasError(f"busca IMAP falhou: {typ}")
    uids = set(_parse_search(data))
    if unseen:
        typ, data = client.uid("SEARCH", "UNSEEN")
        if typ != "OK":
            raise AtlasError(f"busca IMAP falhou: {typ}")
        uids.update(_parse_search(data))
    if cap is not None:
        uids = {uid for uid in uids if uid <= int(cap)}
    return sorted(uids)


def max_uid(client: ImapClient) -> int:
    """Maior UID existente na mailbox selecionada (0 se vazia)."""
    typ, data = client.uid("SEARCH", "ALL")
    if typ != "OK":
        raise AtlasError(f"busca IMAP falhou: {typ}")
    uids = _parse_search(data)
    return max(uids) if uids else 0


def uid_exists(client: ImapClient, uid: int) -> bool:
    """`True` se o UID ainda existe na mailbox selecionada.

    Usado só para distinguir um expurgo entre a busca e o `FETCH` de um
    erro real de leitura.
    """
    typ, data = client.uid("SEARCH", "UID", str(int(uid)))
    if typ != "OK":
        raise AtlasError(f"busca IMAP falhou: {typ}")
    return bool(_parse_search(data))


def _missing_rfc822(uid: int) -> str:
    return f"mensagem sem RFC822: uid {int(uid)}"


_GMAIL_HEADERS = (b"x-gm-msgid", b"x-gm-thrid")
_HEADER_SEPARATOR_RE = re.compile(rb"\r?\n\r?\n")


def _strip_gmail_headers(raw: bytes) -> bytes:
    """Remove `X-GM-MSGID`/`X-GM-THRID` do bloco de cabeçalho.

    O valor do `FETCH` é a identidade do servidor; um header forjado no
    RFC822 não pode vencê-lo. O separador e o corpo são preservados
    byte a byte.
    """
    match = _HEADER_SEPARATOR_RE.search(raw)
    if match is None:
        head, separator, body = raw, b"", b""
    else:
        # O match começa no terminador de linha do último header, então o
        # separador já o carrega: concatenar sem remover um terminador do
        # último header mantido injetaria um CRLF extra no corpo.
        head = raw[: match.start()]
        separator = match.group(0)
        body = raw[match.end() :]

    kept: list[bytes] = []
    skipping = False
    for line in head.splitlines(keepends=True):
        if line[:1] in (b" ", b"\t"):
            if not skipping:
                kept.append(line)
            continue
        name = line.split(b":", 1)[0].strip().lower()
        skipping = name in _GMAIL_HEADERS
        if not skipping:
            kept.append(line)
    cleaned = b"".join(kept)
    if separator:
        if cleaned.endswith(b"\r\n"):
            cleaned = cleaned[:-2]
        elif cleaned.endswith(b"\n"):
            cleaned = cleaned[:-1]
    return cleaned + separator + body


def _inject_gmail_headers(raw: bytes, message: dict) -> bytes:
    """Substitui os X-GM do RFC822 pelos valores autoritativos do `FETCH`.

    Sem `FETCH` (conta sem `X-GM-EXT-1`), nenhum X-GM sobrevive e o parser
    cai para `Message-ID`. Com `FETCH`, só os valores do servidor valem.
    """
    cleaned = _strip_gmail_headers(raw)
    prefix = b""
    for header, key in (
        (b"X-GM-MSGID", "x_gm_msgid"),
        (b"X-GM-THRID", "x_gm_thrid"),
    ):
        value = message.get(key)
        if not value:
            continue
        prefix += header + b": " + str(value).encode("ascii", "ignore") + b"\r\n"
    return prefix + cleaned if prefix else cleaned


def _first_match(pattern: re.Pattern, blob: bytes) -> str | None:
    match = pattern.search(blob)
    if match is None:
        return None
    return match.group(1).decode("ascii", "ignore")


def fetch_message(client: ImapClient, uid: int, *, gmail: bool) -> dict:
    try:
        uid_int = int(uid)
    except (TypeError, ValueError):
        raise AtlasError(f"uid inválido: {uid!r}") from None
    parts = "(RFC822 X-GM-MSGID X-GM-THRID)" if gmail else "(RFC822)"
    try:
        typ, data = client.uid("FETCH", str(uid_int), parts)
    except (imaplib.IMAP4.error, OSError) as exc:
        raise AtlasError(f"FETCH uid {uid_int} falhou: {exc}") from exc
    if typ != "OK":
        raise AtlasError(f"FETCH uid {uid_int} falhou: {typ}")

    raw: bytes | None = None
    head: list[bytes] = []
    for item in data or []:
        if isinstance(item, tuple):
            meta = item[0] if len(item) > 0 else b""
            payload = item[1] if len(item) > 1 else None
            if isinstance(meta, bytes):
                head.append(meta)
            if raw is None and isinstance(payload, bytes):
                raw = payload
        elif isinstance(item, bytes):
            head.append(item)
    if raw is None:
        raise AtlasError(_missing_rfc822(uid_int))

    blob = b" ".join(head)
    return {
        "raw": raw,
        "x_gm_msgid": _first_match(_X_GM_MSGID_RE, blob),
        "x_gm_thrid": _first_match(_X_GM_THRID_RE, blob),
    }


def capabilities(client: ImapClient) -> list[str]:
    typ, data = client.capability()
    if typ != "OK":
        raise AtlasError(f"CAPABILITY falhou: {typ}")
    names: set[str] = set()
    for chunk in data or []:
        if chunk is None:
            continue
        if isinstance(chunk, bytes):
            chunk = chunk.decode("ascii", "ignore")
        names.update(token.upper() for token in str(chunk).split())
    return sorted(names)


def supports_gmail(client: ImapClient) -> bool:
    return "X-GM-EXT-1" in capabilities(client)


# ---------------------------------------------------------------------------
# Ingestão
# ---------------------------------------------------------------------------
def _default_since(dt) -> str:
    return (dt.date() - timedelta(days=DEFAULT_BOOTSTRAP_DAYS)).isoformat()


def _record_failure(cursor: dict, exc: Exception) -> None:
    cursor["consecutive_errors"] = int(cursor.get("consecutive_errors") or 0) + 1
    cursor["last_error"] = str(exc)


def _batch(uids, limit: int):
    return list(uids) if limit < 0 else list(uids[:limit])


def ingest_mailbox(
    store: Store,
    client: ImapClient,
    *,
    account: str,
    mailbox: str,
    limit: int = 50,
    since: str | None = None,
    unseen: bool = True,
    dry_run: bool = False,
    now: str | None = None,
) -> dict:
    """Lê a mailbox em modo shadow e devolve as contagens da rodada."""
    _check_identity(account, mailbox)
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise AtlasError("limit deve ser inteiro")

    control.assert_active(store, "reader")

    # Leitura atômica sem escrita: o dry-run não adquire o lock de mailbox
    # (não cria `state/cursors/*.lock`) e não publica cursor/trigger.
    if dry_run:
        return _ingest_locked(
            store,
            client,
            account=account,
            mailbox=mailbox,
            limit=limit,
            since=since,
            unseen=unseen,
            dry_run=True,
            now=now,
        )

    # Exclusão por `(account, mailbox)`: o cursor é lido e o progresso é
    # publicado sem que outro leitor da mesma mailbox intercale um snapshot
    # mais novo. O `flock` global nunca é segurado durante a rede.
    with _mailbox_lock(store, account, mailbox):
        return _ingest_locked(
            store,
            client,
            account=account,
            mailbox=mailbox,
            limit=limit,
            since=since,
            unseen=unseen,
            dry_run=dry_run,
            now=now,
        )


def _ingest_locked(
    store: Store,
    client: ImapClient,
    *,
    account: str,
    mailbox: str,
    limit: int,
    since: str | None,
    unseen: bool,
    dry_run: bool,
    now: str | None,
) -> dict:
    """Corpo do `ingest_mailbox`, chamado sob o lock por `(account, mailbox)`."""
    now_iso = now or store.now_iso
    dt = parse_dt(now_iso)

    cursor = read_cursor(store, account, mailbox)
    warnings: list[str] = []

    def save(clear_error: bool = False) -> None:
        if not dry_run:
            _write_progress(store, cursor, clear_error=clear_error)

    # Tentativa de poll: só telemetria (relê sob lock, preserva progresso).
    if not dry_run:
        mark_attempt(store, account, mailbox, now=now_iso)

    try:
        typ, _ = client.select(mailbox, readonly=True)
        if typ != "OK":
            raise AtlasError(f"SELECT falhou em {mailbox}: {typ}")
        uidvalidity = get_uidvalidity(client)
        gmail = supports_gmail(client)
    except AtlasError as exc:
        if not dry_run:
            mark_failure(store, account, mailbox, exc, now=now_iso)
        raise

    previous = cursor.get("uidvalidity")
    if previous not in (None, 0) and previous != uidvalidity:
        cursor["uidvalidity"] = uidvalidity
        cursor["last_uid"] = 0
        cursor["bootstrap"] = {
            "done": False,
            "since": None,
            "unseen": bool(unseen),
            "high_water": None,
            "pending": None,
        }
        cursor["last_error"] = None
        warnings.append("uidvalidity_reset")
    else:
        cursor["uidvalidity"] = uidvalidity

    bootstrap = cursor["bootstrap"]
    seen = {(t.get("source"), t.get("external_id")) for t in store.triggers()}
    fetched = 0
    created = 0
    duplicates = 0
    errors = 0
    skipped = 0
    error: str | None = None
    # --limit é um orçamento da rodada inteira: bootstrap e incremental
    # dividem o mesmo saldo, senão `--limit 1` leria 2 mensagens.
    remaining = limit

    def process(uids, *, bootstrap_mode: bool) -> str | None:
        nonlocal fetched, created, duplicates, errors, skipped
        pending = bootstrap["pending"] if bootstrap_mode else None
        for uid in uids:
            try:
                message = fetch_message(client, uid, gmail=gmail)
                parsed = parse_email(
                    _inject_gmail_headers(message["raw"], message),
                    account=account,
                    mailbox=mailbox,
                    uidvalidity=uidvalidity,
                    uid=uid,
                    received_at=now_iso,
                )
                spec = dict(parsed["trigger"])
                spec["meta"] = {**spec["meta"], "shadow": True}
                if not dry_run:
                    ingest.add_trigger(store, spec, actor="reader")
            except (AtlasError, ValueError, OSError) as exc:
                # UID expurgado entre a busca e o FETCH: `SEARCH UID <uid>`
                # confirma a ausência e o cursor pula. Só esse erro tem
                # tratamento especial; BAD/timeout continuam sendo erro.
                if isinstance(exc, AtlasError) and str(exc) == _missing_rfc822(uid):
                    try:
                        missing = not uid_exists(client, uid)
                    except AtlasError:
                        missing = False
                    if missing:
                        skipped += 1
                        if bootstrap_mode:
                            if uid in pending:
                                pending.remove(uid)
                        else:
                            cursor["last_uid"] = max(int(cursor["last_uid"]), uid)
                        save()
                        continue
                errors += 1
                _record_failure(cursor, exc)
                if not dry_run:
                    mark_failure(store, account, mailbox, exc, now=now_iso)
                return str(exc)

            fetched += 1
            key = (spec["source"], spec["external_id"])
            if key in seen:
                duplicates += 1
            else:
                created += 1
                seen.add(key)
            if bootstrap_mode:
                # O UID só sai do pending depois do trigger confirmado.
                pending.remove(uid)
            cursor["last_uid"] = uid
            cursor["last_message_at"] = now_iso
            cursor["last_error"] = None
            cursor["consecutive_errors"] = 0
            save(clear_error=True)
        return None

    if not bootstrap["done"]:
        if bootstrap["pending"] is None:
            window_since = since or bootstrap.get("since")
            if window_since is None:
                window_since = _default_since(dt)
            bootstrap["since"] = window_since
            bootstrap["unseen"] = bool(unseen)
            try:
                # Fronteira antes de qualquer SINCE/UNSEEN: um UID que chega
                # depois da captura fica para a rodada incremental, nunca
                # entra no pending nem empurra o high_water.
                uidnext = get_uidnext(client)
                high_water = uidnext - 1 if uidnext is not None else -1
                if high_water < 0:
                    high_water = max_uid(client)
                selected = bootstrap_uids(
                    client, window_since, bootstrap["unseen"], cap=high_water
                )
            except AtlasError as exc:
                if not dry_run:
                    mark_failure(store, account, mailbox, exc, now=now_iso)
                raise
            bootstrap["high_water"] = high_water
            bootstrap["pending"] = selected
            if not selected:
                bootstrap["done"] = True
                cursor["last_uid"] = high_water
            save()

        batch = _batch(bootstrap["pending"], remaining)
        error = process(batch, bootstrap_mode=True)
        if remaining >= 0:
            remaining = max(0, remaining - len(batch))
        if error is None and not bootstrap["pending"]:
            bootstrap["done"] = True
            cursor["last_uid"] = bootstrap["high_water"] or 0
            save()

    if error is None and bootstrap["done"] and remaining != 0:
        try:
            uids = search_uids(client, last_uid=int(cursor["last_uid"]))
        except AtlasError as exc:
            if not dry_run:
                mark_failure(store, account, mailbox, exc, now=now_iso)
            raise
        error = process(_batch(uids, remaining), bootstrap_mode=False)

    if error is None:
        cursor["last_success_at"] = now_iso
        # Sucesso limpa o erro anterior, inclusive num poll vazio: senão o
        # health fica em warn para sempre depois de uma falha transitória.
        cursor["last_error"] = None
        cursor["consecutive_errors"] = 0
    save(clear_error=error is None)

    return {
        "fetched": fetched,
        "created": created,
        "duplicates": duplicates,
        "errors": errors,
        "skipped": skipped,
        "uidvalidity": uidvalidity,
        "last_uid": int(cursor["last_uid"]),
        "shadow": True,
        "warnings": warnings,
        "error": error,
        "consecutive_errors": int(cursor.get("consecutive_errors") or 0),
    }


# ---------------------------------------------------------------------------
# Shadow report
# ---------------------------------------------------------------------------
def _is_shadow(trigger: dict) -> bool:
    meta = trigger.get("meta") or {}
    return isinstance(meta, dict) and meta.get("shadow") is True


def shadow_report(store: Store, *, date: str | None = None) -> str:
    """Conta os triggers shadow do dia, propõe rota e grava o markdown."""
    day = date or store.dt.date().isoformat()
    try:
        _date.fromisoformat(day)
    except ValueError:
        raise AtlasError(f"data inválida: {day!r}") from None

    # O dia do relatório é o da leitura (created_at), não o `Date:` do e-mail
    # (`received_at`); triggers antigos sem `created_at` caem no fallback.
    def read_day(trigger: dict) -> str:
        stamp = trigger.get("created_at") or trigger.get("received_at")
        return str(stamp or "")[:10]

    rows = [
        trigger
        for trigger in store.triggers()
        if _is_shadow(trigger) and read_day(trigger) == day
    ]
    rows.sort(key=lambda trigger: str(trigger.get("id", "")))
    rules = triggers.load_rules(store.home)

    counts = {"routed": 0, "ignored": 0, "candidate": 0}
    areas: dict[str, int] = {}
    for trigger in rows:
        decision = triggers.decide(trigger, [], rules)
        state = decision["state"]
        counts[state] += 1
        if state == "routed":
            area = decision["route"].get("area") or "sem_rota"
            areas[area] = areas.get(area, 0) + 1
        elif state == "candidate":
            areas["sem_rota"] = areas.get("sem_rota", 0) + 1

    area_items = sorted(areas.items(), key=lambda item: (-item[1], item[0]))
    area_line = ", ".join(f"{name} {count}" for name, count in area_items)
    sample = ", ".join(str(trigger.get("id")) for trigger in rows[:10])

    lines = [
        f"# SHADOW {day}",
        f"- mensagens lidas: {len(rows)}",
        f"- regras: routed {counts['routed']}, ignored {counts['ignored']}, "
        f"candidate {counts['candidate']}",
        f"- áreas propostas: {area_line or 'nenhuma'}",
        f"- amostra para auditoria: {sample or 'nenhuma'}",
    ]
    text = "\n".join(lines) + "\n"

    path = store.home / "reports" / "shadow" / f"{day}.md"
    # Sob o lock: `backup` enumera/arquiva `reports/` com o mesmo lock e nunca
    # vê o arquivo trocado no meio (o tar já é consistente por rename).
    with store.lock():
        write_text(path, text)
    return text
