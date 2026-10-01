"""Command line: `python -m push_site <command> push_site.ini`."""
import argparse
import logging
from pathlib import Path

from aiohttp import web

from push_site.app import create_app
from push_site.config import ConfigError, load_config
from push_site.storage import Store


def serve(config) -> None:
    store = Store(Path(config.db_path))
    app = create_app(config, store)

    async def close_store(app):
        store.close()
    app.on_cleanup.append(close_store)
    # No access log: the reverse proxy keeps one.
    web.run_app(app, host=config.host, port=config.port, access_log=None, print=None)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="python -m push_site")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="run the web server").add_argument("config", type=Path)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        raise SystemExit(f"Configuration error: {exc}")
    if args.command == "serve":
        serve(config)


if __name__ == "__main__":
    main()
