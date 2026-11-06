"""Parser determinístico de e-mail (RFC 822) para o reader em shadow.

M1b.1a: transforma bytes crus em identidade (ocorrência, mensagem e thread)
e em um trigger normalizado. Não usa rede, store, LLM nem relógio de parede.

Decisões de determinismo:

- `received_at` sai sempre como `-03:00`; `Date` válido vence; sem `Date`,
  usa-se `received_at`; sem nenhum dos dois, `1970-01-01T00:00:00-03:00`
  com o warning `received_at_missing`.
- O fallback `sha256` de identidade usa `From/To/Subject/Date` decodificados,
  normalizados e na ordem canônica, mais o corpo completo (antes do limite de
  1 MB), separados por `\\n`. `Date` entra cru (texto do header) para não
  depender de parsing de data.
- `Message-ID`/`References`/`In-Reply-To` são normalizados nas chaves de
  identidade (trim, sem `<>`, domínio em minúsculas); o valor cru fica em
  `headers.*` (`references` preserva a ordem literal).
- O parser é puro: conflito de `Message-ID` e `thread_root_missing` são
  decisões do store/reader; aqui só se normaliza.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser

from .util import collapse_ws as _collapse
from .util import sha256_text as _sha256_text

BRT = timezone(timedelta(hours=-3))
EPOCH = datetime(1970, 1, 1, tzinfo=BRT)
BODY_LIMIT = 1024 * 1024
NO_SUBJECT = "(sem assunto)"

_ANGLE_RE = re.compile(r"<[^<>]+>")
_PREFIX_RE = re.compile(r"^\s*(?:re|fw|fwd)\s*:\s*", re.IGNORECASE)
_DOMAIN_RE = re.compile(r"^([^@]+)@(.+)$")


# ---------------------------------------------------------------------------
# helpers de headers
# ---------------------------------------------------------------------------
def _header(msg, name: str) -> str | None:
    """Header decodificado (policy default já resolve RFC 2047) ou None."""
    values = msg.get_all(name)
    if not values:
        return None
    return _collapse(" ".join(str(value) for value in values))


def _raw_header(msg, name: str) -> str | None:
    """Header cru: exige `msg` parseado com `policy.compat32`."""
    values = msg.get_all(name)
    if not values:
        return None
    return _collapse(" ".join(str(value) for value in values))


def _message_ids(value: str | None) -> list[str]:
    """Message-IDs crus (com `<>`), na ordem literal do header."""
    if not value:
        return []
    found = [item.strip() for item in _ANGLE_RE.findall(value) if item.strip()]
    return found or [item for item in value.split() if item]


def _normalize_message_id(value: str | None) -> str | None:
    if not value:
        return None
    items = _message_ids(value)
    if not items:
        return None
    ident = items[0].strip().strip("<>").strip()
    match = _DOMAIN_RE.match(ident)
    if match:
        ident = f"{match.group(1)}@{match.group(2).lower()}"
    return ident or None


def _addresses(*raw_values: str | None) -> list[str]:
    pairs = getaddresses([value for value in raw_values if value])
    seen: set[str] = set()
    result: list[str] = []
    for _, address in pairs:
        address = address.strip().lower()
        if address and address not in seen:
            seen.add(address)
            result.append(address)
    return result


def _subject_normalized(subject: str) -> str:
    value = _collapse(subject)
    while True:
        stripped = _PREFIX_RE.sub("", value, count=1)
        if stripped == value:
            break
        value = stripped
    return value.casefold()


# ---------------------------------------------------------------------------
# data
# ---------------------------------------------------------------------------
def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(BRT)


def _parse_received_at(value: str) -> datetime:
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"received_at inválido: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BRT)
    return parsed.astimezone(BRT)


# ---------------------------------------------------------------------------
# corpo
# ---------------------------------------------------------------------------
class _HTMLTextExtractor(HTMLParser):
    """Converte HTML em texto simples preservando blocos de parágrafo."""

    _BLOCKS = frozenset(
        {
            "address", "article", "blockquote", "br", "dd", "div", "dl", "dt",
            "h1", "h2", "h3", "h4", "h5", "h6", "hr", "li", "ol", "p", "pre",
            "section", "table", "td", "th", "tr", "ul",
        }
    )
    _SKIP = frozenset({"script", "style", "head", "title"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._BLOCKS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in self._BLOCKS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._chunks.append(data)

    def text(self) -> str:
        return "".join(self._chunks)


def _html_to_text(markup: str) -> str:
    extractor = _HTMLTextExtractor()
    extractor.feed(markup)
    extractor.close()
    text = extractor.text().replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip("\n")


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").strip("\n")


def _part_text(part) -> str:
    try:
        content = part.get_content()
    except (LookupError, UnicodeError):
        payload = part.get_payload(decode=True) or b""
        charset = part.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset, "replace")
        except LookupError:
            return payload.decode("utf-8", "replace")
    if isinstance(content, bytes):
        return content.decode("utf-8", "replace")
    return content


def _is_attachment(part) -> bool:
    return part.get_content_disposition() == "attachment" or part.get_filename() is not None


def _payload_size(part) -> int:
    """Tamanho decodificado sem materializar o binário quando é base64."""
    cte = str(part.get("Content-Transfer-Encoding") or "").strip().lower()
    payload = part.get_payload()
    if cte == "base64" and isinstance(payload, str):
        compact = re.sub(r"[^A-Za-z0-9+/=]", "", payload)
        padding = len(compact) - len(compact.rstrip("="))
        missing = (-len(compact)) % 4  # base64 sem `=` final de alguns clientes
        return max(0, ((len(compact) + missing) // 4) * 3 - (padding + missing))
    decoded = part.get_payload(decode=True)
    return len(decoded) if decoded is not None else 0


def _extract_body(msg, warnings: list[str]) -> tuple[str, list[dict]]:
    plain: list[str] = []
    html: list[str] = []
    attachments: list[dict] = []

    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        if part.is_multipart():
            continue
        if _is_attachment(part):
            size = _payload_size(part)
            filename = part.get_filename()
            if isinstance(filename, bytes):
                filename = filename.decode("utf-8", "replace")
            content_type = part.get_content_type()
            attachments.append(
                {
                    "filename": _collapse(filename) if filename else None,
                    "content_type": content_type,
                    "size": size,
                }
            )
            if content_type == "application/pdf" and "pdf_text_not_extracted" not in warnings:
                warnings.append("pdf_text_not_extracted")
            continue

        content_type = part.get_content_type()
        if content_type == "text/plain":
            text = _normalize_newlines(_part_text(part))
            if text:
                plain.append(text)
        elif content_type == "text/html":
            text = _html_to_text(_part_text(part))
            if text:
                html.append(text)

    if plain:
        return plain[0], attachments
    if html:
        return "\n\n".join(html), attachments
    warnings.append("empty_body")
    return "", attachments


def _truncate_body(body: str) -> tuple[str, bool]:
    encoded = body.encode("utf-8")
    if len(encoded) <= BODY_LIMIT:
        return body, False
    return encoded[:BODY_LIMIT].decode("utf-8", "ignore"), True


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
def _check_identity(account: str, mailbox: str, uidvalidity: int, uid: int) -> None:
    if not isinstance(account, str) or not account.strip():
        raise ValueError("account deve ser string não vazia")
    if not isinstance(mailbox, str) or not mailbox.strip():
        raise ValueError("mailbox deve ser string não vazia")
    for name, value in (("uidvalidity", uidvalidity), ("uid", uid)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{name} deve ser inteiro >= 0")


def parse_email(
    raw: bytes,
    *,
    account: str,
    mailbox: str,
    uidvalidity: int,
    uid: int,
    received_at: str | None = None,
) -> dict:
    """Normaliza bytes RFC 822 em ocorrência + mensagem + thread + trigger.

    Função pura: o mesmo `raw` e os mesmos parâmetros produzem o mesmo dict.
    """
    if not isinstance(raw, bytes):
        raise ValueError("raw deve ser bytes")
    _check_identity(account, mailbox, uidvalidity, uid)

    warnings: list[str] = []
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    msg_raw = BytesParser(policy=policy.compat32).parsebytes(raw)

    subject = _header(msg, "Subject")
    from_display = _header(msg, "From")
    to_display = _header(msg, "To")
    cc_display = _header(msg, "Cc")

    # Evidência crua: encoded-words e `<>` preservados.
    subject_raw = _raw_header(msg_raw, "Subject")
    from_raw = _raw_header(msg_raw, "From")
    date_raw = _raw_header(msg_raw, "Date")
    message_id_raw = _raw_header(msg_raw, "Message-ID")
    references_raw = _raw_header(msg_raw, "References")
    in_reply_to_raw = _raw_header(msg_raw, "In-Reply-To")
    x_gm_msgid = _raw_header(msg_raw, "X-GM-MSGID")
    x_gm_thrid = _raw_header(msg_raw, "X-GM-THRID")

    title = subject or NO_SUBJECT
    actor_list = _addresses(from_display)
    actor = actor_list[0] if actor_list else ""

    # received_at: Date válido > received_at > epoch.
    date_parsed = _parse_date(_header(msg, "Date"))
    if date_parsed is None:
        if date_raw:
            warnings.append("date_invalid")
        if received_at is not None:
            date_parsed = _parse_received_at(received_at)
        else:
            warnings.append("received_at_missing")
            date_parsed = EPOCH
    received_iso = date_parsed.astimezone(BRT).isoformat(timespec="seconds")

    full_body, attachments = _extract_body(msg, warnings)
    body, body_truncated = _truncate_body(full_body)
    if body_truncated:
        warnings.append("body_truncated")

    # Identidade da mensagem.
    message_id = _normalize_message_id(message_id_raw)
    references = _message_ids(references_raw)
    references_norm = [item.strip("<>").strip() for item in references]
    references_norm = [
        f"{m.group(1)}@{m.group(2).lower()}" if (m := _DOMAIN_RE.match(item)) else item
        for item in references_norm
    ]
    in_reply_to = _normalize_message_id(in_reply_to_raw)

    canonical_headers = "\n".join(
        [
            f"from:{_collapse(from_display or '')}",
            f"to:{_collapse(to_display or '')}",
            f"subject:{_collapse(subject or '')}",
            f"date:{_collapse(date_raw or '')}",
        ]
    )
    content_hash = _sha256_text(f"{canonical_headers}\n\n{full_body}")

    if x_gm_msgid:
        message_key = f"email:{account}:gmail:{_collapse(x_gm_msgid)}"
    elif message_id:
        message_key = f"email:{account}:rfc:{message_id}"
    else:
        message_key = f"email:{account}:sha256:{content_hash}"

    if x_gm_thrid:
        thread_key = f"email:{account}:gmail-thrid:{_collapse(x_gm_thrid)}"
    elif references_norm:
        thread_key = f"email:{account}:root:{references_norm[0]}"
    elif in_reply_to:
        thread_key = f"email:{account}:root:{in_reply_to}"
    else:
        thread_key = f"email:{account}:self:{message_key}"

    participants = sorted(_addresses(from_display, to_display, cc_display))
    suggested_thread = (
        f"email:{account}:subject:"
        f"{_sha256_text(_subject_normalized(title) + chr(10) + ','.join(participants))}"
    )

    occurrence_key = f"{account}|{mailbox}|{uidvalidity}|{uid}"
    occurrence = {
        "account": account,
        "mailbox": mailbox,
        "uidvalidity": uidvalidity,
        "uid": uid,
        "occurrence_key": occurrence_key,
    }

    headers = {
        "from": from_display,
        "to": to_display,
        "cc": cc_display,
        "subject": subject,
        "date": date_raw,
        "message_id": message_id_raw,
        "references": references,
        "in_reply_to": in_reply_to_raw,
        "x_gm_msgid": x_gm_msgid,
        "x_gm_thrid": x_gm_thrid,
    }

    trigger = {
        "source": "email",
        "external_id": message_key,
        "thread_key": thread_key,
        "received_at": received_iso,
        "actor": actor,
        "kind": "email",
        "title": title,
        "body": body,
        "body_sha256": _sha256_text(body),
        "body_truncated": body_truncated,
        "untrusted": True,
        "meta": {
            "account": account,
            "mailbox": mailbox,
            "uidvalidity": uidvalidity,
            "uid": uid,
            "message_key": message_key,
            "thread_key": thread_key,
            "content_hash": content_hash,
            "from_raw": from_raw,
            "from_addr": actor,
            "subject_raw": subject_raw,
            "subject_normalized": _subject_normalized(title),
            "headers": {
                "from": from_raw,
                "subject": subject_raw,
                "message-id": message_id_raw,
                "references": references,
                "in-reply-to": in_reply_to_raw,
            },
            "labels": [mailbox],
            "occurrences": [
                {"mailbox": mailbox, "uidvalidity": uidvalidity, "uid": uid}
            ],
        },
    }

    return {
        "occurrence": occurrence,
        "message_key": message_key,
        "thread_key": thread_key,
        "suggested_thread": suggested_thread,
        "trigger": trigger,
        "headers": headers,
        "attachments": attachments,
        "warnings": warnings,
    }
