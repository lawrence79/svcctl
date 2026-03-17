import os
import signal
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from .config import get_log_dir

_SEP = "─" * 60
_LOG_FLUSH_INTERVAL_SECS = 0.1
_LOG_FLUSH_MAX_LINES = 20


class ServiceProcess:
    def __init__(self, name: str, cfg: dict) -> None:
        self.name = name
        for key in ("dir", "cmd"):
            if key not in cfg:
                raise ValueError(f"Service '{name}' is missing required config key: '{key}'")
        self.cfg = cfg
        self.proc: subprocess.Popen | None = None
        self.pid: int | None = None
        self.status = "stopped"
        self.restart_count = 0
        self.started_at: float | None = None
        self.log_file = None
        self._lock = threading.Lock()
        self._stop_flag = False
        # Incremented on every _do_start; each _watch thread holds its own
        # copy so stale threads from before a restart exit cleanly.
        self._gen = 0

    # ── public interface ──────────────────────────────────────────────────────

    def start(self) -> dict:
        with self._lock:
            if self.status == "running":
                return {"ok": False, "msg": f"{self.name} is already running"}
            self._stop_flag = False
            self._do_start()
            return {"ok": True, "msg": f"Started {self.name} (pid {self.pid})"}

    def stop(self) -> dict:
        with self._lock:
            self._stop_flag = True
            if self.status != "running" or not self.proc:
                return {"ok": False, "msg": f"{self.name} is not running"}
            proc = self.proc

        # Snapshot the full descendant tree *before* signalling — once the
        # parent dies, children may be re-parented to PID 1 and harder to find.
        try:
            import psutil
            parent = psutil.Process(proc.pid)
            children = parent.children(recursive=True)
        except Exception:
            children = []

        # SIGTERM the process group (well-behaved children), then any
        # stragglers found via psutil (e.g. nx/lerna daemons that called setsid).
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass

        for child in children:
            try:
                child.send_signal(signal.SIGTERM)
            except Exception:
                pass

        # Wait outside the lock so _watch threads can also finish.
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            # Escalate: SIGKILL the group and any surviving descendants.
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            for child in children:
                try:
                    child.send_signal(signal.SIGKILL)
                except Exception:
                    pass
            try:
                proc.wait()
            except Exception:
                pass

        with self._lock:
            if self.proc is proc:
                self.status = "stopped"
                self.pid = None
                if self.log_file:
                    try:
                        self.log_file.close()
                    except Exception:
                        pass

        return {"ok": True, "msg": f"Stopped {self.name}"}

    def restart(self) -> dict:
        self.stop()
        return self.start()

    def info(self) -> dict:
        uptime = None
        if self.status == "running" and self.started_at:
            uptime = int(time.time() - self.started_at)
        return {
            "name": self.name,
            "status": self.status,
            "pid": self.pid,
            "restart_count": self.restart_count,
            "uptime": uptime,
        }

    def log_path(self) -> Path:
        return get_log_dir() / f"{self.name}.log"

    # ── internals ─────────────────────────────────────────────────────────────

    def _build_cmd(self) -> str:
        cmd = self.cfg["cmd"]
        use_nvm = self.cfg.get("use_nvm")
        if use_nvm is None:
            use_nvm = (Path(self.cfg["dir"]) / ".nvmrc").exists()
        if not use_nvm:
            return cmd
        nvm_dir = os.environ.get("NVM_DIR", str(Path.home() / ".nvm"))
        nvm_sh = Path(nvm_dir) / "nvm.sh"
        return f'bash -l -c \'source "{nvm_sh}" && nvm use && exec {cmd}\''

    def _open_log(self):
        return open(self.log_path(), "a", buffering=1)

    def _build_env(self) -> dict:
        env = os.environ.copy()
        for item in self.cfg.get("env", []):
            if "=" in str(item):
                k, _, v = str(item).partition("=")
                env[k.strip()] = v.strip()
        env_file = self.cfg.get("env_file")
        if env_file:
            env_path = Path(self.cfg["dir"]) / env_file
            if env_path.exists():
                for line in env_path.read_text().splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    # Strip optional 'export ' prefix
                    if line.startswith("export "):
                        line = line[7:].strip()
                    if "=" not in line:
                        continue
                    k, _, v = line.partition("=")
                    k = k.strip()
                    v = v.strip()
                    # Quoted values — preserve internal whitespace, strip outer quotes
                    if len(v) >= 2 and v[0] in ('"', "'") and v[-1] == v[0]:
                        v = v[1:-1]
                    else:
                        # Strip inline comments on unquoted values
                        v = v.split(" #")[0].rstrip()
                    if k:
                        env[k] = v
        return env

    def _do_start(self) -> None:
        """Must be called with self._lock held."""
        self._gen += 1
        gen = self._gen
        cwd = str(Path(self.cfg["dir"]).resolve())
        cmd = self._build_cmd()
        env = self._build_env()
        log_f = self._open_log()
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_f.write(f"\n{_SEP}\n[svcctl] Starting {self.name} at {ts}\n{_SEP}\n")
        log_f.flush()

        proc = subprocess.Popen(
            cmd,
            shell=True,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            universal_newlines=True,
            start_new_session=True,  # own process group → killpg is safe
        )

        self.proc = proc
        self.pid = proc.pid
        self.status = "running"
        self.started_at = time.time()
        self.log_file = log_f

        threading.Thread(
            target=self._pipe_writer, args=(proc.stdout, log_f), daemon=True
        ).start()
        threading.Thread(
            target=self._watch, args=(gen, proc, log_f), daemon=True
        ).start()

    def _pipe_writer(self, pipe, log_f) -> None:
        """Read process stdout/stderr and write timestamped lines to log."""
        last_flush = time.monotonic()
        pending_lines = 0
        try:
            for raw in pipe:
                ts = datetime.now().strftime("%H:%M:%S")
                try:
                    log_f.write(f"[{ts}] {raw.rstrip(chr(10))}\n")
                    pending_lines += 1
                    now = time.monotonic()
                    if pending_lines >= _LOG_FLUSH_MAX_LINES or (now - last_flush) >= _LOG_FLUSH_INTERVAL_SECS:
                        log_f.flush()
                        last_flush = now
                        pending_lines = 0
                except Exception:
                    break
        except Exception:
            pass
        finally:
            if pending_lines:
                try:
                    log_f.flush()
                except Exception:
                    pass
            pipe.close()

    def _watch(self, gen: int, proc: subprocess.Popen, log_f) -> None:
        """Monitor the process and trigger auto-restart when appropriate."""
        proc.wait()

        with self._lock:
            # A newer _do_start already replaced this generation — bail out.
            if gen != self._gen:
                log_f.close()
                return
            if self._stop_flag:
                self.status = "stopped"
                self.pid = None
                return
            exit_code = proc.returncode
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            try:
                log_f.write(
                    f"\n[svcctl] {self.name} exited (code {exit_code}) at {ts}\n"
                )
                log_f.flush()
            except Exception:
                pass
            self.status = "crashed"
            self.pid = None

        if self.cfg.get("auto_restart", True) and not self._stop_flag and gen == self._gen:
            delay = self.cfg.get("restart_delay", 2)
            time.sleep(delay)
            with self._lock:
                if not self._stop_flag and gen == self._gen:
                    self.restart_count += 1
                    self._do_start()
