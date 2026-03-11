import atexit
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
            for key in ("dir", "cmd"):
                if key not in svc:
                    print(f"[error] Service '{name}' is missing required key '{key}' in config.", file=sys.stderr)
                    sys.exit(1)
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
        server.listen(10)
        atexit.register(lambda: (sp.unlink(missing_ok=True), server.close()))

        def _shutdown(*_):
            for svc in self.services.values():
                try:
                    svc.stop()
                except Exception:
                    pass
            sys.exit(0)

        signal.signal(signal.SIGTERM, _shutdown)
        signal.signal(signal.SIGINT, _shutdown)

        for svc in self.services.values():
            svc.start()

        while True:
            try:
                conn, _ = server.accept()
                threading.Thread(target=self._handle, args=(conn,), daemon=True).start()
            except Exception:
                break

    def _handle(self, conn: socket.socket) -> None:
        try:
            msg = recv_msg(conn)
            if not msg:
                return
            resp = self._dispatch(msg)
            send_msg(conn, resp)
        finally:
            conn.close()

    def _dispatch(self, msg: dict) -> dict:
        action = msg.get("action")
        name = msg.get("name")

        if action == "status":
            with self._lock:
                return {"services": [s.info() for s in self.services.values()]}
        if action == "start":
            with self._lock:
                return {"results": [s.start() for s in self._resolve(name)]}
        if action == "stop":
            with self._lock:
                return {"results": [s.stop() for s in self._resolve(name)]}
        if action == "restart":
            with self._lock:
                return {"results": [s.restart() for s in self._resolve(name)]}
        if action == "ping":
            return {"pong": True}
        if action == "reload":
            return self._do_reload()
        if action == "remove":
            return self._do_remove(name)

        return {"error": f"Unknown action: {action}"}

    def _do_reload(self) -> dict:
        try:
            new_cfgs = self._parse_config()
        except Exception as e:
            return {"ok": False, "error": str(e)}

        with self._lock:
            current_keys = set(self.services.keys())
            new_keys = set(new_cfgs.keys())

            removed = []
            for name in current_keys - new_keys:
                try:
                    self.services[name].stop()
                except Exception:
                    pass
                del self.services[name]
                removed.append(name)

            added = []
            for name in new_keys - current_keys:
                svc = ServiceProcess(name, new_cfgs[name])
                svc.start()
                self.services[name] = svc
                added.append(name)

        return {"ok": True, "added": added, "removed": removed}

    def _do_remove(self, name: str | None) -> dict:
        if not name:
            return {"ok": False, "error": "No service name provided"}
        with self._lock:
            if name not in self.services:
                return {"ok": False, "error": f"Service '{name}' not found"}
            svc = self.services.pop(name)

        try:
            svc.stop()
        except Exception:
            pass

        try:
            with open(self.config_path) as f:
                cfg = yaml.safe_load(f) or {}
            if name in cfg.get("services", {}):
                del cfg["services"][name]
                with open(self.config_path, "w") as f:
                    yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)
        except Exception:
            pass

        return {"ok": True}

    def _resolve(self, name: str | None) -> list[ServiceProcess]:
        if name is None or name == "all":
            return list(self.services.values())
        if name in self.services:
            return [self.services[name]]
        return []
