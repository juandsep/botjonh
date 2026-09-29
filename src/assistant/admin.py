"""Admin CLI. Usage: ``python -m assistant.admin add-owner <chat_id> <nombre>``."""

from __future__ import annotations

import argparse

from assistant.services import state


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m assistant.admin")
    commands = parser.add_subparsers(dest="command", required=True)
    add_owner = commands.add_parser("add-owner", help="create or promote the owner")
    add_owner.add_argument("chat_id")
    add_owner.add_argument("nombre")
    args = parser.parse_args(argv)
    state.upsert_user(args.chat_id, args.nombre, rol="owner")
    print("owner saved")


if __name__ == "__main__":
    main()
