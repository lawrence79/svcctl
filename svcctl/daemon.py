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
        with open(self.config_path) as f:
            cfg = yaml.safe_load(f)

        self.services: dict[str, ServiceProcess] = {}
        for name, svc in cfg.get("services", {}).items():
            svc = dict(svc)
            for key in ("dir", "cmd"):
                if key not in svc:
                    print(f"[error] Service '{name}' is missing required key '{key}' in config.", file=sys.stderr)
                    sys.exit(1)
            svc["dir"] = str((self.config_dir / svc["dir"]).resolve())
            self.services[name] = ServiceProcess(name, svc)

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
            return {"services": [s.info() for s in self.services.values()]}
        if action == "start":
            return {"results": [s.start() for s in self._resolve(name)]}
        if action == "stop":
            return {"results": [s.stop() for s in self._resolve(name)]}
        if action == "restart":
            return {"results": [s.restart() for s in self._resolve(name)]}
        if action == "ping":
            return {"pong": True}

        return {"error": f"Unknown action: {action}"}

    def _resolve(self, name: str | None) -> list[ServiceProcess]:
        if name is None or name == "all":
            return list(self.services.values())
        if name in self.services:
            return [self.services[name]]
        return []
