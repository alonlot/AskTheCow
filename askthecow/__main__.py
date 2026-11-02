"""Command line: serve | calibrate | sync | ask | brief."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .config import Settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="askthecow", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="Run the HTTP server (and the background sync loop).")
    sub.add_parser("calibrate", help="Read all commits of the branch and write the brief from scratch.")
    sub.add_parser("sync", help="Fetch the branch and update the knowledge base with new commits.")
    sub.add_parser("brief", help="Print the current brief.")
    ask = sub.add_parser("ask", help="Ask one question from the terminal.")
    ask.add_argument("question")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()

    if args.cmd == "serve":
        import uvicorn

        from .server import create_app

        uvicorn.run(create_app(settings), host=settings.host, port=settings.port)
        return 0

    from .server import Services, answer_question

    async def run() -> int:
        svc = Services(settings)
        if args.cmd in ("calibrate", "sync"):
            await svc.kb.sync(force_full=args.cmd == "calibrate")
            brief = svc.db.latest_brief()
            print(f"Done. {svc.db.commit_note_count()} commits in the knowledge base. "
                  f"Brief: {len(brief['content']) if brief else 0} characters at {brief['commit_sha'][:10] if brief else '-'}.")
            return 0
        if args.cmd == "brief":
            brief = svc.db.latest_brief()
            print(brief["content"] if brief else "No brief yet. Run: askthecow calibrate")
            return 0 if brief else 1
        chat = svc.db.create_chat("cli", args.question[:60])
        queue: asyncio.Queue = asyncio.Queue()
        task = asyncio.create_task(answer_question(svc, chat["id"], [], args.question, queue))
        while True:
            ev = await queue.get()
            if ev["type"] == "text":
                print(ev["text"], end="", flush=True)
            elif ev["type"] == "turn" and ev["turn"] > 1:
                print("\n", file=sys.stderr)
            elif ev["type"] == "tool":
                print(f"[{ev['name']} {ev['input']}]", file=sys.stderr)
            elif ev["type"] == "status":
                print(f"[{ev['text']}]", file=sys.stderr)
            elif ev["type"] in ("done", "error"):
                print()
                if ev["type"] == "error":
                    print(ev["error"], file=sys.stderr)
                break
        await task
        return 0

    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
