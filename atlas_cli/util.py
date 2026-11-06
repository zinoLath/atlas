"""Utilidades básicas: datas, IDs, slugs, JSONL atômico e hashes."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import unicodedata
from datetime import datetime
from pathlib import Path

_SLUG_RE = re.compile(r"[^a-z0-9]+")


class AtlasError(Exception):
    """Erro de uso/estado. Sai com código 2 por padrão."""

    def __init__(self, message: str, code: int = 2) -> None:
        super().__init__(message)
        self.code = code


def parse_dt(value: str | datetime) -> datetime:
    """Converte ISO8601 em datetime com timezone (assume local se ausente)."""
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise AtlasError(f"timestamp inválido: {value!r}") from exc
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def now_dt(value: str | None = None) -> datetime:
    if value is None:
        return datetime.now().astimezone()
    return parse_dt(value)


def slugify(text: str) -> str:
    """Slug ASCII minúsculo: '2025 Seq2Seq TCN' -> '2025-seq2seq-tcn'."""
    normalized = unicodedata.normalize("NFKD", text)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii").lower()
    return _SLUG_RE.sub("-", ascii_text).strip("-")


def read_env_file(path: Path) -> dict[str, str]:
    """Lê um `KEY=VALOR` simples (`export` e aspas aceitos); ausente = {}."""
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise AtlasError(f"secrets ilegível: {path}: {exc}") from exc
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_jsonl(path: Path) -> list[dict]:
    """Lê um JSONL; linhas em branco são ignoradas."""
    if not path.exists():
        return []
    rows: list[dict] = []
    with path.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise AtlasError(f"{path}:{lineno}: JSON inválido ({exc.msg})") from exc
            if not isinstance(obj, dict):
                raise AtlasError(f"{path}:{lineno}: esperado objeto JSON")
            rows.append(obj)
    return rows


def fsync_dir(path: Path) -> None:
    """Sincroniza o diretório para tornar durável um `rename`/`truncate`."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Temp exclusivo por escritor: dois processos publicando o mesmo alvo
    # (ex.: blob content-addressed) não truncam um ao outro; `os.replace`
    # publica os bytes completos de um deles.
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=path.parent
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    fsync_dir(path.parent)


def write_jsonl(path: Path, rows: list[dict]) -> None:
    """Escreve JSONL canônico: chaves ordenadas, compacto, tmp+rename."""
    text = "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for row in rows
    )
    _atomic_write(path, text)


def write_text(path: Path, text: str) -> None:
    _atomic_write(path, text)


def collapse_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def title_tokens(title: str) -> set[str]:
    normalized = unicodedata.normalize("NFKD", title)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii").lower()
    return set(re.findall(r"[a-z0-9]+", ascii_text))
