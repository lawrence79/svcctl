import sys


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "daemon":
        import argparse
        from .config import get_config_path
        from .daemon import Daemon
        p = argparse.ArgumentParser()
        p.add_argument("--config", default=None)
        args, _ = p.parse_known_args(sys.argv[2:])
        Daemon(args.config or str(get_config_path())).run()
    else:
        from .tui import run_tui
        run_tui()


if __name__ == "__main__":
    main()
