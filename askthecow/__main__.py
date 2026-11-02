"""Command line: serve | calibrate | sync | brief | ask."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from .config import Settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="askthecow", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="Run the HTTP server, the sync loop and the configured channels.")
    sub.add_parser("calibrate", help="Read all commits of the branch and write the brief from scratch.")
    sub.add_parser("sync", help="Fetch the branch and update the knowledge base with new commits.")
    sub.add_parser("brief", help="Print the current brief.")
    ask = sub.add_parser("ask", help="Ask one question from the terminal.")
    ask.add_argument("question")
    ask.add_argument("--thread", help="Continue the CLI conversation with this name.")
    ask.add_argument("--json", action="store_true", help="Print the answer as JSON.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()

    if args.cmd == "serve":
        import uvicorn

        from .server import create_app

        uvicorn.run(create_app(settings), host=settings.host, port=settings.port)
        return 0

    from .service import AskService, Inbound

    async def run() -> int:
        service = AskService(settings)
        try:
            if args.cmd in ("calibrate", "sync"):
                await service.kb.sync(force_full=args.cmd == "calibrate")
                brief = service.db.latest_brief()
                print(
                    f"Done. {service.db.commit_note_count()} commits in the knowledge base. "
                    f"Brief: {len(brief['content']) if brief else 0} characters "
                    f"at {brief['commit_sha'][:10] if brief else '-'}."
                )
                return 0
            if args.cmd == "brief":
                brief = service.db.latest_brief()
                print(brief["content"] if brief else "No brief yet. Run: askthecow calibrate")
                return 0 if brief else 1

            async def show(ev: dict) -> None:
                if args.json:
                    return
                if ev["type"] == "text":
                    print(ev["text"], end="", flush=True)
                elif ev["type"] == "turn":
                    print(file=sys.stderr)
                elif ev["type"] == "tool":
                    print(f"[{ev['name']} {json.dumps(ev['input'])[:150]}]", file=sys.stderr)
                elif ev["type"] == "status":
                    print(f"[{ev['text']}]", file=sys.stderr)

            out = await service.ask(Inbound(text=args.question, channel="cli", user="cli", thread_id=args.thread), show)
            if args.json:
                print(json.dumps(out.__dict__, default=str, indent=2))
            else:
                print()
                if out.error:
                    print(out.text, file=sys.stderr)
            return 1 if out.error else 0
        finally:
            service.close()

    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
