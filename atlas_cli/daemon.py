"""atlasd: processo residente e escritor único do estado ATLAS.

O CLI fala com ele por um unix socket (JSON por linha). A serialização das
escritas acontece aqui, com `threading.Lock`; `Store.transaction`/`flock`
continuam como segunda camada de proteção.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import socket
import stat
import struct
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from .store import DEFAULT_HOME, Store
from .util import AtlasError, write_text

SOCKET_NAME = "atlasd.sock"
SOCKET_POINTER_NAME = "atlasd.socket"
PID_NAME = "atlasd.pid"
LOG_NAME = "atlasd.log"
LOG_MAX_BYTES = 1_048_576
COMMAND_TIMEOUT_SECONDS = 120.0
CLIENT_TIMEOUT_SECONDS = COMMAND_TIMEOUT_SECONDS + 30.0
REQUEST_MAX_BYTES = 8 * 1024 * 1024
PEER_READ_TIMEOUT_SECONDS = 10.0
SOCKET_MODE = 0o600

DISABLE_ENV = "ATLAS_DAEMON_DISABLE"
REQUIRE_ENV = "ATLAS_REQUIRE_DAEMON"
IN_DAEMON_ENV = "ATLAS_IN_DAEMON"


def default_home() -> Path:
    base = os.environ.get("ATLAS_HOME") or DEFAULT_HOME
    return Path(base).expanduser().resolve()


def socket_path(home: str | Path | None = None) -> Path:
    """Socket do daemon: `ATLAS_SOCKET`, ponteiro `state/atlasd.socket` ou default."""
    root = Path(home).expanduser().resolve() if home else default_home()
    override = os.environ.get("ATLAS_SOCKET")
    if override:
        return Path(override).expanduser().resolve()
    pointer = root / "state" / SOCKET_POINTER_NAME
    try:
        recorded = pointer.read_text(encoding="utf-8").strip()
    except OSError:
        recorded = ""
    if recorded:
        return Path(recorded).expanduser().resolve()
    return root / "state" / SOCKET_NAME


def pid_path(home: str | Path | None = None) -> Path:
    root = Path(home).expanduser().resolve() if home else default_home()
    return root / "state" / PID_NAME


def split_now(argv: list[str]) -> tuple[str | None, list[str]]:
    """Separa o `--now` da linha de comando (valor fica para o campo `now`)."""
    now: str | None = None
    rest: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--now" and index + 1 < len(argv):
            now = argv[index + 1]
            index += 2
            continue
        if token.startswith("--now="):
            now = token.split("=", 1)[1]
            index += 1
            continue
        rest.append(token)
        index += 1
    return now, rest


def _encode(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n"


def _read_message(conn: socket.socket, timeout: float) -> dict:
    conn.settimeout(timeout)
    chunks = bytearray()
    while not chunks.endswith(b"\n"):
        chunk = conn.recv(65536)
        if not chunk:
            break
        chunks.extend(chunk)
        if len(chunks) > REQUEST_MAX_BYTES:
            raise AtlasError("mensagem grande demais")
    if not chunks.strip():
        raise AtlasError("conexão encerrada sem resposta")
    try:
        message = json.loads(chunks.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AtlasError(f"mensagem JSON inválida: {exc}") from exc
    if not isinstance(message, dict):
        raise AtlasError("mensagem deve ser um objeto JSON")
    return message


def exchange(path: Path, payload: dict, timeout: float = CLIENT_TIMEOUT_SECONDS) -> dict:
    """Envia uma requisição e devolve a resposta. Erros de conexão propagam."""
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(timeout)
    try:
        conn.connect(str(path))
        conn.sendall(_encode(payload))
        return _read_message(conn, timeout)
    finally:
        conn.close()


def _require_daemon() -> bool:
    return os.environ.get(REQUIRE_ENV) == "1"


def _remove_orphan(path: Path) -> None:
    try:
        if stat.S_ISSOCK(path.stat().st_mode):
            path.unlink()
    except OSError:
        pass


def forward(argv: list[str]) -> int | None:
    """Encaminha `argv` ao daemon. Devolve o exit code, ou None para rodar local."""
    if os.environ.get(IN_DAEMON_ENV) or os.environ.get(DISABLE_ENV):
        return None
    path = socket_path()
    if not path.exists():
        if _require_daemon():
            print(f"atlas: daemon indisponível (socket ausente: {path})", file=sys.stderr)
            return 2
        return None

    now, rest = split_now(argv)
    try:
        response = exchange(path, {"argv": rest, "now": now})
    except (FileNotFoundError, ConnectionRefusedError, NotADirectoryError):
        _remove_orphan(path)
        if _require_daemon():
            print(f"atlas: daemon indisponível (socket não responde: {path})", file=sys.stderr)
            return 2
        return None
    except TimeoutError:
        print(f"atlas: daemon não respondeu em {CLIENT_TIMEOUT_SECONDS:.0f}s", file=sys.stderr)
        return 124
    except AtlasError as exc:
        # Conexão caiu/resposta inválida no meio do comando: o estado é
        # desconhecido, então nunca reexecutar localmente.
        print(f"atlas: daemon: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"atlas: falha ao falar com o daemon: {exc}", file=sys.stderr)
        return 2

    sys.stdout.write(response.get("stdout") or "")
    sys.stderr.write(response.get("stderr") or "")
    code = response.get("code")
    return code if isinstance(code, int) else 2


def _ping(path: Path) -> dict | None:
    try:
        response = exchange(path, {"cmd": "ping"}, timeout=5.0)
    except (OSError, AtlasError):
        return None
    return response if response.get("code") == 0 else None


def command(argv: list[str]) -> int:
    """`atlas daemon status|stop|start` — executado no processo do CLI."""
    action = argv[0] if argv else "status"
    if len(argv) > 1:
        raise AtlasError(f"argumentos demais para 'daemon {action}'")
    if action in ("-h", "--help", "help"):
        print("uso: atlas daemon [status|stop|start]", file=sys.stdout)
        print("status  socket, pid e resposta a um ping", file=sys.stdout)
        print("stop    pede shutdown e espera o socket sumir", file=sys.stdout)
        print("start   não spawna: o systemd inicia bin/atlasd", file=sys.stdout)
        return 0
    path = socket_path()
    if action == "status":
        print(f"socket: {path}")
        pid = _read_pid()
        print(f"pid: {pid if pid is not None else '-'}")
        if _ping(path) is None:
            print("status: indisponível")
            return 1
        print("status: ok")
        return 0
    if action == "stop":
        if not path.exists():
            print("daemon não está rodando")
            return 0
        try:
            response = exchange(path, {"cmd": "shutdown"})
        except (FileNotFoundError, ConnectionRefusedError, NotADirectoryError):
            _remove_orphan(path)
            print("daemon não está rodando")
            return 0
        sys.stdout.write(response.get("stdout") or "")
        deadline = time.monotonic() + 10.0
        while path.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        if path.exists():
            print(f"atlas: socket não sumiu: {path}", file=sys.stderr)
            return 1
        print("daemon encerrado")
        return 0
    if action == "start":
        raise AtlasError("iniciar o daemon é papel do systemd: use bin/atlasd")
    raise AtlasError(f"ação de daemon desconhecida: {action}")


def _read_pid() -> int | None:
    try:
        raw = pid_path().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _daemon_uid() -> int:
    return os.getuid()


def _peer_uid(conn: socket.socket) -> int:
    raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", raw)
    return uid


class Daemon:
    """Servidor unix socket serializado; um comando ATLAS por vez."""

    def __init__(self, home: str | Path | None = None, socket_file: str | Path | None = None) -> None:
        self.home = Path(home).expanduser().resolve() if home else default_home()
        if socket_file:
            self.sock_path = Path(socket_file).expanduser().resolve()
        elif os.environ.get("ATLAS_SOCKET"):
            self.sock_path = Path(os.environ["ATLAS_SOCKET"]).expanduser().resolve()
        else:
            self.sock_path = self.home / "state" / SOCKET_NAME
        self.pid_path = self.home / "state" / PID_NAME
        self.pointer_path = self.home / "state" / SOCKET_POINTER_NAME
        self.log_path = self.home / "state" / LOG_NAME
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._server: socket.socket | None = None
        self._threads: list[threading.Thread] = []

    # ---- ciclo de vida -------------------------------------------------
    def run(self) -> int:
        os.environ["ATLAS_HOME"] = str(self.home)
        os.environ[IN_DAEMON_ENV] = "1"
        store = Store(home=self.home)
        store.init()
        store.recover()
        try:
            self._bind()
        except (OSError, AtlasError) as exc:
            self._log(f"não foi possível abrir {self.sock_path}: {exc}")
            return 1
        write_text(self.pid_path, f"{os.getpid()}\n")
        write_text(self.pointer_path, f"{self.sock_path}\n")
        self._install_signals()
        self._log(f"pid {os.getpid()} escutando em {self.sock_path} (home {self.home})")
        try:
            self._serve()
        finally:
            self._cleanup()
            self._log("encerrado")
        return 0

    def stop(self) -> None:
        self._stop.set()

    def _bind(self) -> None:
        self.sock_path.parent.mkdir(parents=True, exist_ok=True)
        if self.sock_path.exists():
            if stat.S_ISSOCK(self.sock_path.stat().st_mode) and _ping(self.sock_path):
                raise AtlasError("já existe um daemon escutando neste socket")
            self.sock_path.unlink()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        previous = os.umask(0o177)
        try:
            server.bind(str(self.sock_path))
        finally:
            os.umask(previous)
        os.chmod(self.sock_path, SOCKET_MODE)
        server.listen(64)
        server.settimeout(0.2)
        self._server = server

    def _install_signals(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, self._on_signal)

    def _on_signal(self, signum, frame) -> None:
        self._log(f"sinal {signum}")
        self._stop.set()

    def _cleanup(self) -> None:
        if self._server is not None:
            self._server.close()
            self._server = None
        _remove_orphan(self.sock_path)
        try:
            if self.pid_path.read_text(encoding="utf-8").strip() == str(os.getpid()):
                self.pid_path.unlink()
        except OSError:
            pass
        try:
            if self.pointer_path.read_text(encoding="utf-8").strip() == str(self.sock_path):
                self.pointer_path.unlink()
        except OSError:
            pass
        for thread in self._threads:
            if thread is not threading.current_thread():
                thread.join(timeout=1.0)

    def _serve(self) -> None:
        assert self._server is not None
        while not self._stop.is_set():
            try:
                conn, _address = self._server.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            thread = threading.Thread(target=self._handle, args=(conn,), daemon=True)
            thread.start()
            self._threads = [item for item in self._threads if item.is_alive()]
            self._threads.append(thread)

    # ---- conexões -------------------------------------------------------
    def _handle(self, conn: socket.socket) -> None:
        with conn:
            try:
                if not self._peer_allowed(conn):
                    self._log("conexão recusada: uid diferente do daemon")
                    _send(conn, _error("atlasd: uid não autorizado"))
                    return
                request = _read_message(conn, PEER_READ_TIMEOUT_SECONDS)
            except (OSError, AtlasError) as exc:
                _send(conn, _error(f"atlasd: {exc}"))
                return

            code, out, err = self._dispatch(request)
            _send(conn, {"code": code, "stdout": out, "stderr": err})

    def _peer_allowed(self, conn: socket.socket) -> bool:
        try:
            return _peer_uid(conn) == _daemon_uid()
        except OSError as exc:
            self._log(f"SO_PEERCRED indisponível: {exc}")
            return False

    def _dispatch(self, request: dict) -> tuple[int, str, str]:
        command = request.get("cmd")
        if command == "ping":
            return 0, f"pong {os.getpid()}\n", ""
        argv = request.get("argv")
        if command == "shutdown" or (isinstance(argv, list) and argv[:2] == ["daemon", "stop"]):
            self._log("shutdown solicitado")
            self._stop.set()
            return 0, "atlasd: encerrando\n", ""
        if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
            return 2, "", "atlasd: campo 'argv' deve ser uma lista de strings\n"
        now = request.get("now")
        if now is not None and not isinstance(now, str):
            return 2, "", "atlasd: campo 'now' deve ser string ISO8601\n"
        full = ["--now", now, *argv] if now else list(argv)
        return self._run_with_timeout(full)

    def _run_with_timeout(self, argv: list[str]) -> tuple[int, str, str]:
        box: dict[str, tuple[int, str, str]] = {}

        def worker() -> None:
            box["result"] = self._execute(argv)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        thread.join(COMMAND_TIMEOUT_SECONDS)
        if thread.is_alive():
            self._log(f"timeout ({COMMAND_TIMEOUT_SECONDS:.0f}s): {' '.join(argv)}")
            return 124, "", f"atlasd: comando excedeu {COMMAND_TIMEOUT_SECONDS:.0f}s\n"
        return box["result"]

    def _execute(self, argv: list[str]) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        started = time.monotonic()
        with self._lock:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                try:
                    from .cli import main

                    code = main(argv)
                except SystemExit as exc:  # erro de uso do argparse
                    code = exc.code if isinstance(exc.code, int) else 2
                except BaseException as exc:  # nunca derruba o daemon
                    code = 2
                    print(f"atlasd: erro interno: {exc!r}", file=sys.stderr)
        elapsed = time.monotonic() - started
        self._log(f"{' '.join(argv) or '<vazio>'} -> {code} ({elapsed:.3f}s)")
        return code, stdout.getvalue(), stderr.getvalue()

    # ---- log ------------------------------------------------------------
    def _log(self, message: str) -> None:
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        line = f"{stamp} atlasd[{os.getpid()}] {message}\n"
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                if self.log_path.stat().st_size > LOG_MAX_BYTES:
                    os.replace(self.log_path, self.log_path.with_name(LOG_NAME + ".1"))
            except FileNotFoundError:
                pass
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(line)
        except OSError:
            sys.stderr.write(line)


def _error(message: str) -> dict:
    return {"code": 2, "stdout": "", "stderr": message.rstrip("\n") + "\n"}


def _send(conn: socket.socket, response: dict) -> None:
    try:
        conn.sendall(_encode(response))
    except OSError:
        pass
