"""Backup do estado: tar.gz determinístico + manifest com sha256.

Inclui tudo que não é reconstruível: `state/` (JSONL canônicos),
`raw/blobs/`, `demands/`, `router/`, `areas/`, `kb/`, `policy/`,
`BULLETIN.md` e `reports/`.

Fora do backup: `state/hub.db` e afins `*.db`/`*.db-wal`/`*.db-shm`
(SQLite é derivado dos JSONL), `state/.lock`, sockets `atlasd.sock`/
`atlasd.socket`, temporários `.atlas-backup-*.tmp`, `.git` e `tests/`
(o tar só percorre as raízes listadas, então qualquer outra coisa fora
delas fica de fora por construção).
"""

from __future__ import annotations

import hashlib
import json
import os
import tarfile
from datetime import datetime
from pathlib import Path

from .store import Store
from .util import AtlasError, fsync_dir, iso, parse_dt, write_text

BACKUP_GLOB = "atlas-backup-*.tar.gz"
DEFAULT_KEEP = 7
TAR_SUFFIX = ".tar.gz"
EXCLUDED_STATE_NAMES = frozenset(
    {"atlasd.sock", "atlasd.socket", ".lock", "atlasd.pid", "atlasd.log"}
)
EXCLUDED_STATE_SUFFIXES = (".db", ".db-wal", ".db-shm")
#: Log rotacionado do daemon (`atlasd.log.1`, ...): efêmero como o log.
EXCLUDED_STATE_PREFIXES = ("atlasd.log.",)
BACKUP_TMP_SUFFIX = ".tmp"


def default_dir(store: Store) -> Path:
    return store.home / "backups"


def _manifest_path(tar_path: Path) -> Path:
    return tar_path.with_name(tar_path.name[: -len(TAR_SUFFIX)] + ".json")


def _excluded(name: str) -> bool:
    if name in EXCLUDED_STATE_NAMES or name.endswith(EXCLUDED_STATE_SUFFIXES):
        return True
    # `atlasd.log.1` (rotação) é efêmero como o próprio log.
    if name.startswith(EXCLUDED_STATE_PREFIXES):
        return True
    # Lock por `(account, mailbox)` em `state/cursors/`: não é estado.
    if name.endswith(".lock"):
        return True
    return name.endswith(BACKUP_TMP_SUFFIX)


def _entries(store: Store) -> list[tuple[Path, str]]:
    """Pares `(arquivo, arcname)` ordenados; só arquivos regulares."""
    pairs: list[tuple[Path, str]] = []

    def add_tree(root: Path, prefix: str, *, skip_state: bool = False) -> None:
        if not root.is_dir():
            return
        for item in sorted(root.rglob("*")):
            if not item.is_file() or item.is_symlink():
                continue
            if skip_state and _excluded(item.name):
                continue
            # Qualquer temporário de publicador (`state/hub.db.*.tmp`,
            # `raw/blobs/<sha>.<rand>.tmp`, `reports/.../<date>.md.*.tmp`)
            # pode sumir a qualquer momento: nunca entra no tar.
            if item.name.endswith(BACKUP_TMP_SUFFIX):
                continue
            relative = item.relative_to(root).as_posix()
            pairs.append((item, f"{prefix}/{relative}" if prefix else relative))

    add_tree(store.state_dir, "state", skip_state=True)
    add_tree(store.home / "raw" / "blobs", "raw/blobs")
    add_tree(store.home / "demands", "demands")
    add_tree(store.home / "router", "router")
    add_tree(store.home / "areas", "areas")
    add_tree(store.home / "kb", "kb")
    add_tree(store.home / "policy", "policy")
    add_tree(store.home / "reports", "reports")
    bulletin = store.home / "BULLETIN.md"
    if bulletin.is_file():
        pairs.append((bulletin, "BULLETIN.md"))

    pairs.sort(key=lambda pair: pair[1])
    return pairs


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify(path: Path, expected: int) -> None:
    try:
        with tarfile.open(path, "r:gz") as archive:
            members = archive.getmembers()
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise AtlasError(f"backup inválido: {path} ({exc})") from exc
    if len(members) != expected:
        raise AtlasError(
            f"backup inválido: {path} tem {len(members)} membros, esperado {expected}"
        )


def latest(store: Store) -> Path | None:
    """Tar de backup mais novo em `backups/`, ou `None`."""
    directory = default_dir(store)
    if not directory.is_dir():
        return None
    candidates = sorted(path for path in directory.glob(BACKUP_GLOB) if path.is_file())
    return candidates[-1] if candidates else None


def _prune(directory: Path, keep: int) -> None:
    tars = sorted(path for path in directory.glob(BACKUP_GLOB) if path.is_file())
    for old in tars[:-keep] if len(tars) > keep else []:
        manifest = _manifest_path(old)
        old.unlink(missing_ok=True)
        manifest.unlink(missing_ok=True)


def create(
    store: Store,
    *,
    out_dir: Path | None = None,
    keep: int = DEFAULT_KEEP,
    now: str | None = None,
) -> dict:
    if isinstance(keep, bool) or not isinstance(keep, int) or keep < 1:
        raise AtlasError("--keep deve ser inteiro positivo")

    dt = parse_dt(now) if now else store.dt
    directory = Path(out_dir).expanduser() if out_dir else default_dir(store)
    try:
        created = not directory.exists()
        directory.mkdir(parents=True, exist_ok=True)
        if created:
            os.chmod(directory, 0o700)
    except OSError as exc:
        raise AtlasError(f"backup falhou ao preparar {directory}: {exc}") from exc

    # Captura sob o lock dos escritores: enumeração + leitura + tar fechado e
    # verificado acontecem sem transação concorrente alterando `state/`.
    # Blobs são content-addressed (imutáveis), então a cópia é consistente.
    with store.lock():
        try:
            pairs = _entries(store)
        except OSError as exc:
            raise AtlasError(f"backup falhou ao listar {store.home}: {exc}") from exc
        stamp = dt.strftime("%Y%m%dT%H%M%S")
        tar_path = directory / f"atlas-backup-{stamp}{TAR_SUFFIX}"
        tmp_path = directory / f".atlas-backup-{stamp}.tmp"

        try:
            added = 0
            with tarfile.open(tmp_path, "w:gz", format=tarfile.PAX_FORMAT) as archive:
                for source, arcname in pairs:
                    # Um membro canônico que some entre a enumeração e a
                    # abertura é violação de contrato: escritores publicam
                    # sob o mesmo lock e blobs são imutáveis. `*.tmp` já é
                    # excluído antes de enumerar. Um tar/manifest de sucesso
                    # com membros a menos seria um backup incompleto válido;
                    # deixar `FileNotFoundError` cair no tratamento de falha
                    # abaixo remove o `.tmp` e não publica nada.
                    archive.add(source, arcname=arcname, recursive=False)
                    added += 1
            _verify(tmp_path, added)
            fd = os.open(tmp_path, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            size = tmp_path.stat().st_size
            checksum = _sha256(tmp_path)
            os.replace(tmp_path, tar_path)
            fsync_dir(directory)
        except (tarfile.TarError, OSError) as exc:
            tmp_path.unlink(missing_ok=True)
            raise AtlasError(f"backup falhou: {exc}") from exc

        manifest = {
            "created_at": iso(dt),
            "entries": added,
            "bytes": size,
            "sha256": checksum,
        }
        manifest_path = _manifest_path(tar_path)
        try:
            write_text(
                manifest_path,
                json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            )
        except OSError as exc:
            raise AtlasError(f"backup falhou ao gravar {manifest_path}: {exc}") from exc
    try:
        _prune(directory, keep)
    except OSError as exc:
        raise AtlasError(
            f"backup criado em {tar_path}, mas a retenção falhou: {exc}"
        ) from exc
    return {
        "path": str(tar_path),
        "manifest": str(manifest_path),
        "entries": added,
        "bytes": size,
        "sha256": checksum,
        "created_at": iso(dt),
    }


def age_hours(tar_path: Path, dt: datetime) -> float:
    """Idade em horas: `created_at` do manifest, senão mtime do tar."""
    manifest_path = _manifest_path(tar_path)
    if manifest_path.is_file():
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            created_at = data.get("created_at") if isinstance(data, dict) else None
            if isinstance(created_at, str):
                return (dt - parse_dt(created_at)).total_seconds() / 3600
        except (OSError, UnicodeDecodeError, ValueError, AtlasError):
            pass
    mtime = datetime.fromtimestamp(tar_path.stat().st_mtime, tz=dt.tzinfo)
    return (dt - mtime).total_seconds() / 3600
