import atexit
from concurrent.futures import ThreadPoolExecutor
import errno
import os
import signal
import socket
import sys
import threading
from pathlib import Path

import yaml

from .config import get_pid_file, get_socket_path
from .ipc import recv_msg, send_msg
from .service import ServiceProcess


class Daemon:
    def __init__(self, config_path: str) -> None:
        self.config_path = Path(config_path).resolve()
        self.config_dir = self.config_path.parent
        self._lock = threading.Lock()
        self._handler_pool = ThreadPoolExecutor(max_workers=12)
        self._status_revision = 0
        self._last_status_signature: tuple[tuple[str, str, int | None, int], ...] | None = None

        self.services: dict[str, ServiceProcess] = {}
        for name, svc in self._parse_config().items():
            self.services[name] = ServiceProcess(name, svc)

    def _parse_config(self) -> dict[str, dict]:
        with open(self.config_path) as f:
            cfg = yaml.safe_load(f) or {}
        root_raw = cfg.get("root")
        root = Path(root_raw).expanduser().resolve() if root_raw else self.config_dir
        result = {}
        for name, svc in cfg.get("services", {}).items():
            svc = dict(svc)
            svc["dir"] = str((root / svc["dir"]).resolve())
            result[name] = svc
        return result

    def run(self) -> None:
        pid_file = get_pid_file()
        pid_file.write_text(str(os.getpid()))
        atexit.register(lambda: pid_file.unlink(missing_ok=True))

        sp = get_socket_path()
        if sp.exists():
            sp.unlink()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(sp))
        os.chmod(str(sp), 0o600)
        server.listen(10)
        atexit.register(lambda: (sp.unlink(missing_ok=True), server.close()))

        def _shutdown(*_):
            for svc in self.services.values():
                try:
                    svc.stop()
                except Exception:
                    pass
            self._handler_pool.shutdown(wait=False, cancel_futures=True)
            sys.exit(0)

        signal.signal(signal.SIGTERM, _shutdown)
        signal.signal(signal.SIGINT, _shutdown)

        for svc in self.services.values():
            svc.start()

        while True:
            try:
                conn, _ = server.accept()
                self._handler_pool.submit(self._handle, conn)
            except OSError as exc:
                if exc.errno == errno.EBADF:
                    break
                continue
            except RuntimeError:
                break
            except Exception:
                break

    def _handle(self, conn: socket.socket) -> None:
        try:
            msg = recv_msg(conn)
            if not msg:
                return
            try:
                resp = self._dispatch(msg)
            except Exception as e:
                resp = {"ok": False, "error": str(e)}
            send_msg(conn, resp)
        finally:
            conn.close()

    def _dispatch(self, msg: dict) -> dict:
        action = msg.get("action")
        name = msg.get("name")

        if action == "status":
            with self._lock:
                services = [s.info() for s in self.services.values()]
                signature = tuple(
                    (svc["name"], svc["status"], svc["pid"], svc["restart_count"])
                    for svc in services
                )
                if signature != self._last_status_signature:
                    self._last_status_signature = signature
                    self._status_revision += 1
                return {"services": services, "revision": self._status_revision}
        if action in ("start", "stop", "restart"):
            if name is not None and name != "all" and name not in self.services:
                return {"results": [{"ok": False, "msg": f"Service '{name}' not found"}]}
            with self._lock:
                svcs = self._resolve(name)
            # Lock released — start/stop/restart can block and have their own per-service lock.
            if action == "start":
                return {"results": [s.start() for s in svcs]}
            if action == "stop":
                return {"results": [s.stop() for s in svcs]}
            return {"results": [s.restart() for s in svcs]}
        if action == "ping":
            return {"pong": True}
        if action == "reload":
            return self._do_reload()
        if action == "remove":
            return self._do_remove(name)
        if action == "add":
            return self._do_add(msg)

        return {"error": f"Unknown action: {action}"}

    def _do_reload(self) -> dict:
        try:
            new_cfgs = self._parse_config()
        except Exception as e:
            return {"ok": False, "error": str(e)}

        to_stop = []
        to_start_new = []
        to_restart = []
        with self._lock:
            current_keys = set(self.services.keys())
            new_keys = set(new_cfgs.keys())

            removed = []
            for name in current_keys - new_keys:
                to_stop.append(self.services.pop(name))
                removed.append(name)

            added = []
            for name in new_keys - current_keys:
                svc = ServiceProcess(name, new_cfgs[name])
                self.services[name] = svc
                to_start_new.append(svc)
                added.append(name)

            updated = []
            for name in current_keys & new_keys:
                if self.services[name].cfg != new_cfgs[name]:
                    self.services[name].cfg = new_cfgs[name]
                    to_restart.append(self.services[name])
                    updated.append(name)

        for svc in to_start_new:
            try:
                svc.start()
            except Exception:
                pass

        for svc in to_restart:
            threading.Thread(target=svc.restart, daemon=True).start()

        for svc in to_stop:
            def _stop(s=svc):
                try:
                    s.stop()
                except Exception:
                    pass
            threading.Thread(target=_stop, daemon=True).start()

        return {"ok": True, "added": added, "removed": removed, "updated": updated}

    def _do_remove(self, name: str | None) -> dict:
        if not name:
            return {"ok": False, "error": "No service name provided"}
        with self._lock:
            if name not in self.services:
                return {"ok": False, "error": f"Service '{name}' not found"}

        # Write config first — if this fails, in-memory state is left unchanged.
        try:
            with open(self.config_path) as f:
                cfg = yaml.safe_load(f) or {}
            if name in cfg.get("services", {}):
                del cfg["services"][name]
                with open(self.config_path, "w") as f:
                    yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)
        except Exception as e:
            return {"ok": False, "error": f"Could not update config: {e}"}

        with self._lock:
            svc = self.services.pop(name, None)

        # Stop asynchronously — svc.stop() can block up to ~10s waiting for
        # the process to die, which would cause the IPC call to time out.
        if svc:
            def _stop_quietly():
                try:
                    svc.stop()
                except Exception:
                    pass
            threading.Thread(target=_stop_quietly, daemon=True).start()
        return {"ok": True}

    def _do_add(self, msg: dict) -> dict:
        name = msg.get("name")
        entry = msg.get("entry")
        if not name or not entry:
            return {"ok": False, "error": "name and entry required"}
        with self._lock:
            if name in self.services:
                return {"ok": False, "error": f"Service '{name}' already exists"}

        try:
            with open(self.config_path) as f:
                cfg = yaml.safe_load(f) or {}
            cfg.setdefault("services", {})[name] = entry
            with open(self.config_path, "w") as f:
                yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)
        except Exception as e:
            return {"ok": False, "error": str(e)}

        try:
            new_cfgs = self._parse_config()
        except Exception as e:
            return {"ok": False, "error": str(e)}

        if name not in new_cfgs:
            return {"ok": False, "error": f"Service '{name}' not found after config write"}

        with self._lock:
            if name in self.services:
                return {"ok": False, "error": f"Service '{name}' already exists"}
            svc = ServiceProcess(name, new_cfgs[name])
            self.services[name] = svc

        try:
            svc.start()
        except Exception as e:
            with self._lock:
                self.services.pop(name, None)
            return {"ok": False, "error": f"Failed to start service: {e}"}
        return {"ok": True}

    def _resolve(self, name: str | None) -> list[ServiceProcess]:
        if name is None or name == "all":
            return list(self.services.values())
        if name in self.services:
            return [self.services[name]]
        return []
