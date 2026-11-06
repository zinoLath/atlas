"""Sender Signal via `signal-cli` (note-to-self por padrão).

Configuração em `~/.config/atlas/secrets.env` ou no ambiente; o ambiente
vence o arquivo:

- `ATLAS_SIGNAL_NUMBER` (obrigatório): número registrado no signal-cli;
- `ATLAS_SIGNAL_TO` (opcional): destinatário; ausente = note-to-self;
- `ATLAS_SIGNAL_CLI` (default `signal-cli`);
- `ATLAS_SIGNAL_CONFIG` (default `~/.local/share/signal-cli`).

Nenhum erro inclui a mensagem enviada, o número ou o conteúdo do stderr
cru: os segredos são redigidos e o texto é truncado.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from .util import AtlasError, read_env_file

DEFAULT_ENV_PATH = Path("~/.config/atlas/secrets.env")
DEFAULT_CLI = "signal-cli"
DEFAULT_CONFIG = Path("~/.local/share/signal-cli")
SEND_TIMEOUT_SECONDS = 60
STDERR_LIMIT = 200

ENV_KEYS = (
    "ATLAS_SIGNAL_NUMBER",
    "ATLAS_SIGNAL_TO",
    "ATLAS_SIGNAL_CLI",
    "ATLAS_SIGNAL_CONFIG",
)

#: Valores que nunca aparecem em mensagem de erro.
_SECRET_ENV = frozenset({"ATLAS_SIGNAL_NUMBER", "ATLAS_SIGNAL_TO"})


def _config(env_path: Path | None = None) -> dict:
    """Número, destino, binário e diretório de configuração do signal-cli."""
    path = Path(env_path).expanduser() if env_path else DEFAULT_ENV_PATH.expanduser()
    values = read_env_file(path)
    for key in ENV_KEYS:
        if key in os.environ:
            values[key] = os.environ[key]

    number = (values.get("ATLAS_SIGNAL_NUMBER") or "").strip()
    if not number:
        raise AtlasError(
            f"ATLAS_SIGNAL_NUMBER ausente (defina em {path} ou no ambiente)"
        )
    return {
        "number": number,
        "to": (values.get("ATLAS_SIGNAL_TO") or "").strip(),
        "cli": (values.get("ATLAS_SIGNAL_CLI") or "").strip() or DEFAULT_CLI,
        "config": Path(
            (values.get("ATLAS_SIGNAL_CONFIG") or "").strip() or DEFAULT_CONFIG
        ).expanduser(),
    }


def load_number(env_path: Path | None = None) -> str:
    """Número do signal-cli, sem ecoar o valor em erros."""
    return _config(env_path)["number"]


def _argv(config: dict, message: str) -> list[str]:
    argv = [
        config["cli"],
        "-a",
        config["number"],
        "--config",
        str(config["config"]),
        "send",
    ]
    if config["to"]:
        argv.extend(["-m", message, config["to"]])
    else:
        argv.extend(["--note-to-self", "-m", message])
    return argv


_PHONE_TOKEN_RE = re.compile(r"\+?\d[\d\s().-]{4,}\d")


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value)


def _scrub_phones(text: str, secrets) -> str:
    """Redige o número mesmo quando o stderr o formata (+55 11 9...)."""
    targets = {_digits(secret) for secret in secrets if len(_digits(secret)) >= 6}
    if not targets:
        return text

    def replace(match: re.Match) -> str:
        return "<redigido>" if _digits(match.group(0)) in targets else match.group(0)

    return _PHONE_TOKEN_RE.sub(replace, text)


def _short_error(stderr: str, config: dict, message: str) -> str:
    """stderr sem segredos, em uma linha, truncado."""
    text = stderr or ""
    for secret in (message, config["number"], config["to"]):
        if secret:
            text = text.replace(secret, "<redigido>")
    text = _scrub_phones(text, (config["number"], config["to"]))
    text = " ".join(text.split())
    if len(text) > STDERR_LIMIT:
        text = text[:STDERR_LIMIT] + "..."
    return text or "sem stderr"


def send(message: str, digest: dict) -> str:
    """Envia `message` e devolve `signal:<id>`; `digest` não é usado no argv.

    Timeout devolve `"unknown"`: o processo pode ter entregado antes de
    estourar, então a notificação vira `delivery_unknown` e só é reenviada
    com `outbox --retry --force`.
    """
    config = _config()
    argv = _argv(config, message)
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=SEND_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError:
        raise AtlasError(f"{config['cli']} não encontrado; instale/registre") from None
    except subprocess.TimeoutExpired:
        return "unknown"

    if result.returncode != 0:
        raise AtlasError(
            f"signal-cli falhou (rc={result.returncode}): "
            f"{_short_error(result.stderr, config, message)}"
        )

    lines = (result.stdout or "").strip().splitlines()
    return f"signal:{lines[-1] if lines else 'ok'}"
