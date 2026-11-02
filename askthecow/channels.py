"""Input channels: how questions get in and answers get out, other than the web chat.

To add a channel (for example email):

    # mycompany/mail_channel.py
    from askthecow.channels import Channel
    from askthecow.service import Inbound

    class MailChannel(Channel):
        name = "email"

        async def run(self) -> None:
            while True:
                for mail in await fetch_new_mails():            # your IMAP / Graph API code
                    answer = await self.service.ask(Inbound(
                        text=mail.body,
                        channel=self.name,
                        user=mail.sender,
                        thread_id=mail.thread_id,              # replies go to the same conversation
                        title=mail.subject,
                        instructions="Write the answer as a plain-text email.",
                        meta={"message_id": mail.message_id},
                    ))
                    await send_reply(mail, answer.text)        # your SMTP code
                await asyncio.sleep(60)

Then start the server with:

    ASKTHECOW_CHANNELS=mycompany.mail_channel:MailChannel askthecow serve

The server starts `run()` as a background task, and cancels it at shutdown. If `run()` fails, it is
logged and started again after a delay.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Settings
    from .service import AskService

log = logging.getLogger(__name__)
RESTART_DELAY_SECONDS = 30


class Channel:
    """Base class for a channel. Subclasses set `name` and implement `run()`."""

    name = "channel"

    def __init__(self, service: "AskService", settings: "Settings"):
        self.service = service
        self.settings = settings

    async def run(self) -> None:
        """Receive questions, call `self.service.ask(...)`, send answers. Runs until cancelled."""
        raise NotImplementedError


def load_channel(spec: str, service: "AskService", settings: "Settings") -> Channel:
    """Make a channel from "package.module:ClassName"."""
    module_name, _, class_name = spec.partition(":")
    if not module_name or not class_name:
        raise ValueError(f"Channel must be 'module:ClassName', got {spec!r}")
    cls = getattr(importlib.import_module(module_name), class_name)
    if not (isinstance(cls, type) and issubclass(cls, Channel)):
        raise TypeError(f"{spec} is not a subclass of askthecow.channels.Channel")
    return cls(service, settings)


async def supervise(channel: Channel) -> None:
    """Run a channel forever. Restart it when it fails."""
    while True:
        try:
            await channel.run()
            log.info("channel %s stopped", channel.name)
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("channel %s failed; restart in %s s", channel.name, RESTART_DELAY_SECONDS)
            await asyncio.sleep(RESTART_DELAY_SECONDS)
