"""Admin CLI.

``python -m assistant.admin add-owner <chat_id> <nombre>``
``python -m assistant.admin migrate-gifs <owner_chat_id>``: copies the owner's
old per-user ``gifs/{chat_id}`` lists into the shared catalog's ``general``.
"""

from __future__ import annotations

import argparse

from assistant.services import state


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m assistant.admin")
    commands = parser.add_subparsers(dest="command", required=True)
    add_owner = commands.add_parser("add-owner", help="create or promote the owner")
    add_owner.add_argument("chat_id")
    add_owner.add_argument("nombre")
    migrate = commands.add_parser(
        "migrate-gifs", help="copy the owner's GIFs into the shared catalog"
    )
    migrate.add_argument("chat_id")
    args = parser.parse_args(argv)
    if args.command == "migrate-gifs":
        print(f"{state.migrate_gifs(args.chat_id)} gifs migrated")
        return
    state.upsert_user(args.chat_id, args.nombre, rol="owner")
    print("owner saved")


if __name__ == "__main__":
    main()
