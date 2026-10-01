"""Command line: `python -m push_site <command> push_site.ini ...`.

Run the admin commands on the server as the site's user; there's no admin
web interface.
"""
import argparse
import asyncio
import logging
import os
from datetime import datetime
from pathlib import Path

from aiohttp import web

from push_site.app import create_app
from push_site.config import ConfigError, load_config
from push_site.push import Pusher, generate_vapid_key, load_vapid_key, public_key
from push_site.storage import Store


def build_app(config, store: Store) -> web.Application:
    """The app with its push worker running alongside it."""
    vapid = load_vapid_key(Path(config.vapid_key_file))
    pusher = Pusher(store, vapid, config.vapid_subject)
    app = create_app(config, store, pusher.enqueue, public_key(vapid))

    async def run_pusher(app):
        task = asyncio.create_task(pusher.run())
        yield
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    app.cleanup_ctx.append(run_pusher)
    return app


def serve(config, store: Store) -> None:
    # No access log: the reverse proxy keeps one.
    web.run_app(build_app(config, store), host=config.host, port=config.port, access_log=None, print=None)


def invite(config, store: Store, args) -> None:
    token = store.create_invite(args.label, args.uses, args.days * 86400)
    uses = "once" if args.uses == 1 else f"{args.uses} times"
    print(f"Invite for {args.label!r}, usable {uses} within {args.days:g} days:")
    print(f"{config.public_origin}/#invite={token}")


def _when(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def devices(config, store: Store, args) -> None:
    rows = store.devices()
    if not rows:
        print("No devices.")
    for row in rows:
        print(f"{row['id']:>4}  {row['label']}  (added {_when(row['created_at'])}, last seen {_when(row['last_seen'])})")


def revoke(config, store: Store, args) -> None:
    if not store.delete_device(args.device_id):
        raise SystemExit(f"No device {args.device_id}.")
    print(f"Device {args.device_id} revoked.")


def vapid(config, store: Store, args) -> None:
    key = generate_vapid_key(Path(config.vapid_key_file))
    print(f"Created {config.vapid_key_file}; keep it private. Public key:\n{public_key(key)}")


def _positive(kind):
    def parse(value: str):
        number = kind(value)
        if not 0 < number <= 10_000:
            raise argparse.ArgumentTypeError("must be between 0 and 10000")
        return number
    return parse


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="python -m push_site")
    commands = parser.add_subparsers(dest="command", required=True)

    def command(name: str, help: str) -> argparse.ArgumentParser:
        sub = commands.add_parser(name, help=help)
        sub.add_argument("config", type=Path, help="path to push_site.ini")
        return sub

    command("serve", "run the web server")
    sub = command("invite", "print a new invite link")
    sub.add_argument("--label", required=True, help="who or what it's for, shown by `devices`")
    sub.add_argument("--uses", type=_positive(int), default=1, help="how many devices may use it (default 1)")
    sub.add_argument("--days", type=_positive(float), default=7, help="how long it's valid for (default 7)")
    command("devices", "list signed-in devices")
    command("revoke", "sign a device out and drop its subscription").add_argument("device_id", type=int)
    command("vapid", "create the site's VAPID key (once, before the first serve)")
    args = parser.parse_args(argv)
    if os.geteuid() == 0:
        # Files it created would be root's, and the site couldn't write them.
        raise SystemExit("Run this as the site's own user, not root (e.g. sudo -u pushsite ...).")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        config = load_config(args.config)
        store = Store(Path(config.db_path))
        try:
            if args.command == "serve":
                serve(config, store)
            else:
                commands = {"invite": invite, "devices": devices, "revoke": revoke, "vapid": vapid}
                commands[args.command](config, store, args)
        finally:
            store.close()
    except ConfigError as exc:
        raise SystemExit(f"Configuration error: {exc}")


if __name__ == "__main__":
    main()
