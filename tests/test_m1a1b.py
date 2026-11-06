"""Testes do M1a.1b: daemon atlasd, protocolo unix socket e encaminhamento."""

from __future__ import annotations

import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

BIN = REPO / "bin" / "atlas"
ATLASD = REPO / "bin" / "atlasd"
TMP_ROOT = REPO / "tests" / ".tmp"
NOW = "2026-11-01T14:00:00-03:00"
CLEAN_ENV = (
    "ATLAS_DAEMON_DISABLE",
    "ATLAS_REQUIRE_DAEMON",
    "ATLAS_IN_DAEMON",
    "ATLAS_TEST_CRASH_AFTER",
)


class M1a1bTestCase(unittest.TestCase):
    def setUp(self) -> None:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.home = Path(tempfile.mkdtemp(prefix="atlas-m1a1b-", dir=TMP_ROOT))
        self.daemons: list[subprocess.Popen] = []
        proc = self.run_atlas("init")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def tearDown(self) -> None:
        for proc in self.daemons:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=10)
            for pipe in (proc.stdout, proc.stderr):
                if pipe is not None:
                    pipe.close()
        shutil.rmtree(self.home, ignore_errors=True)

    # ---- helpers ------------------------------------------------------
    def env(self, **extra: str) -> dict:
        env = {key: value for key, value in os.environ.items() if key not in CLEAN_ENV}
        env["ATLAS_HOME"] = str(self.home)
        env.update(extra)
        return env

    def run_atlas(
        self, *args: str, now: str = NOW, env_extra: dict | None = None
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(BIN), "--now", now, *args],
            capture_output=True,
            text=True,
            env=self.env(**(env_extra or {})),
            check=False,
        )

    def start_daemon(self, home: Path | None = None, *extra: str) -> subprocess.Popen:
        home = home or self.home
        proc = subprocess.Popen(
            [sys.executable, str(ATLASD), "--home", str(home), *extra],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self.env(),
        )
        self.daemons.append(proc)
        self.wait_for(
            lambda: (home / "state" / "atlasd.pid").exists() and self._socket_ready(home),
            proc,
        )
        return proc

    def _socket_ready(self, home: Path) -> bool:
        pointer = home / "state" / "atlasd.socket"
        if pointer.exists():
            return Path(pointer.read_text(encoding="utf-8").strip()).exists()
        return (home / "state" / "atlasd.sock").exists()

    def wait_for(self, predicate, proc: subprocess.Popen | None = None, timeout: float = 10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            if proc is not None and proc.poll() is not None:
                out, err = proc.communicate()
                self.fail(f"daemon morreu (code {proc.returncode}): {out}\n{err}")
            time.sleep(0.02)
        self.fail("timeout esperando condição")

    def jsonl(self, name: str) -> list[dict]:
        text = (self.home / "state" / name).read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    # ---- 1. serialização sob concorrência -----------------------------
    def test_daemon_serializes_concurrent_adds(self) -> None:
        self.start_daemon()
        procs = [
            subprocess.Popen(
                [
                    sys.executable, str(BIN), "--now", NOW,
                    "add", "--area", "projetos", "--title", f"Concorrente {index}",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=self.env(),
            )
            for index in range(8)
        ]
        for proc in procs:
            out, err = proc.communicate(timeout=120)
            self.assertEqual(proc.returncode, 0, f"{out}\n{err}")

        demands = self.jsonl("demands.jsonl")
        events = self.jsonl("events.jsonl")
        self.assertEqual(len(demands), 8)
        self.assertEqual(len({row["id"] for row in demands}), 8)
        self.assertEqual(len(events), 8)
        self.assertEqual(sorted(row["seq"] for row in events), list(range(1, 9)))

        self.assertIn("doctor ok\n", self.run_atlas("doctor").stdout)
        log = (self.home / "state" / "atlasd.log").read_text(encoding="utf-8")
        self.assertEqual(log.count("add --area projetos"), 8)

    # ---- 2. modo do socket --------------------------------------------
    def test_daemon_socket_mode(self) -> None:
        self.start_daemon()
        mode = stat.S_IMODE((self.home / "state" / "atlasd.sock").stat().st_mode)
        self.assertEqual(oct(mode), oct(0o600))

    # ---- 3. fallback local --------------------------------------------
    def test_daemon_fallback_when_absent(self) -> None:
        self.assertFalse((self.home / "state" / "atlasd.sock").exists())
        proc = self.run_atlas("add", "--area", "projetos", "--title", "Local")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("new projetos: Local", proc.stdout)
        self.assertEqual(len(self.jsonl("demands.jsonl")), 1)

        # socket órfão (bind + close sem listener) é removido e o CLI roda local
        stale = self.home / "state" / "atlasd.sock"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(stale))
        server.close()
        self.assertTrue(stale.exists())
        proc = self.run_atlas("add", "--area", "projetos", "--title", "Órfão")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(self.jsonl("demands.jsonl")), 2)

    # ---- 4. daemon obrigatório ----------------------------------------
    def test_daemon_requires_when_set(self) -> None:
        proc = self.run_atlas(
            "add", "--area", "projetos", "--title", "Sem daemon",
            env_extra={"ATLAS_REQUIRE_DAEMON": "1"},
        )
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertIn("daemon indisponível", proc.stderr)
        self.assertEqual(self.jsonl("demands.jsonl"), [])

    # ---- 5. status/stop -----------------------------------------------
    def test_daemon_status_stop(self) -> None:
        daemon = self.start_daemon()
        socket_file = self.home / "state" / "atlasd.sock"
        pid_file = self.home / "state" / "atlasd.pid"
        self.assertEqual(int(pid_file.read_text(encoding="utf-8").strip()), daemon.pid)

        status = self.run_atlas("daemon", "status")
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIn(f"socket: {socket_file}", status.stdout)
        self.assertIn(f"pid: {daemon.pid}", status.stdout)
        self.assertIn("status: ok", status.stdout)

        stop = self.run_atlas("daemon", "stop")
        self.assertEqual(stop.returncode, 0, stop.stderr)
        self.assertIn("daemon encerrado", stop.stdout)
        self.assertFalse(socket_file.exists())
        self.assertFalse(pid_file.exists())
        daemon.wait(timeout=10)
        self.assertEqual(daemon.returncode, 0)

        status = self.run_atlas("daemon", "status")
        self.assertEqual(status.returncode, 1)
        self.assertIn("status: indisponível", status.stdout)

    def test_daemon_usage_error_keeps_running(self) -> None:
        self.start_daemon()
        bad = self.run_atlas("add", "--area", "projetos")
        self.assertEqual(bad.returncode, 2)
        self.assertIn("--title", bad.stderr)
        good = self.run_atlas("add", "--area", "projetos", "--title", "Depois do erro")
        self.assertEqual(good.returncode, 0, good.stderr)
        self.assertEqual(len(self.jsonl("demands.jsonl")), 1)

    # ---- 6. uid estranho ----------------------------------------------
    def test_daemon_rejects_foreign_uid(self) -> None:
        import atlas_cli.daemon as daemon_mod

        server = daemon_mod.Daemon(home=self.home)
        thread = threading.Thread(target=server.run, daemon=True)
        real_uid = daemon_mod._daemon_uid
        saved = {key: os.environ.get(key) for key in ("ATLAS_HOME", "ATLAS_IN_DAEMON")}
        try:
            thread.start()
            self.wait_for(lambda: server.sock_path.exists())
            with unittest.mock.patch.object(daemon_mod, "_daemon_uid", lambda: real_uid() + 1):
                response = daemon_mod.exchange(server.sock_path, {"cmd": "ping"})
            self.assertEqual(response["code"], 2)
            self.assertIn("uid", response["stderr"])

            response = daemon_mod.exchange(server.sock_path, {"cmd": "ping"})
            self.assertEqual(response["code"], 0)
            self.assertIn("pong", response["stdout"])
        finally:
            server.stop()
            thread.join(timeout=10)
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self.assertFalse(thread.is_alive())


    # ---- 7. queda do daemon no meio do comando ------------------------
    def test_daemon_broken_reply_is_clean_error(self) -> None:
        path = self.home / "state" / "atlasd.sock"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(path))
        listener.listen(1)

        def accept_and_close() -> None:
            conn, _address = listener.accept()
            conn.recv(65536)  # consome a requisição e fecha sem responder
            conn.close()

        thread = threading.Thread(target=accept_and_close)
        thread.start()
        try:
            proc = self.run_atlas("add", "--area", "projetos", "--title", "Queda")
        finally:
            thread.join(timeout=10)
            listener.close()

        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertIn("sem resposta", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertEqual(self.jsonl("demands.jsonl"), [])

    # ---- 8. socket fora do padrão --------------------------------------
    def test_daemon_socket_pointer_for_custom_socket(self) -> None:
        outside = Path(tempfile.mkdtemp(prefix="atlasd-sock-", dir=TMP_ROOT))
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        custom = outside / "custom.sock"
        self.start_daemon(self.home, "--socket", str(custom))
        pointer = self.home / "state" / "atlasd.socket"
        self.assertTrue(custom.exists())
        self.assertEqual(pointer.read_text(encoding="utf-8").strip(), str(custom))

        status = self.run_atlas("daemon", "status")
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIn(f"socket: {custom}", status.stdout)
        self.assertIn("status: ok", status.stdout)

        add = self.run_atlas("add", "--area", "projetos", "--title", "Socket custom")
        self.assertEqual(add.returncode, 0, add.stderr)
        self.assertEqual(len(self.jsonl("demands.jsonl")), 1)

        stop = self.run_atlas("daemon", "stop")
        self.assertEqual(stop.returncode, 0, stop.stderr)
        self.assertFalse(custom.exists())
        self.assertFalse(pointer.exists())

    # ---- 9. timeout do comando ----------------------------------------
    def test_daemon_command_timeout_returns_124(self) -> None:
        import atlas_cli.daemon as daemon_mod

        server = daemon_mod.Daemon(home=self.home)
        with unittest.mock.patch.object(daemon_mod, "COMMAND_TIMEOUT_SECONDS", 0.2):
            with unittest.mock.patch.object(
                server, "_execute", lambda argv: (time.sleep(1.0), (0, "", ""))[1]
            ):
                code, out, err = server._run_with_timeout(["list"])
        self.assertEqual(code, 124)
        self.assertEqual(out, "")
        self.assertIn("excedeu", err)


if __name__ == "__main__":
    unittest.main()
