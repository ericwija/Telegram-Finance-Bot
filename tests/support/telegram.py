"""A fake Bot API transport and update builders: the real PTB stack, no network.

``FakeBotApi`` is plugged into ``ApplicationBuilder.request(...)``; every Bot API call is
recorded and answered locally. Errors are produced the way Telegram produces them (HTTP status
plus ``{"ok": false, ...}``), so PTB raises its real exception types.
"""

import asyncio
import itertools
import json
from collections.abc import Callable
from typing import Any

from telegram import Update
from telegram._utils.defaultvalue import DEFAULT_NONE
from telegram._utils.types import ODVInput
from telegram.ext import Application
from telegram.request import BaseRequest, RequestData

BOT_ID = 5_000_000_001
BOT_USERNAME = "finbot_test_bot"
BOT_TOKEN = "5000000001:AAtest-not-a-real-token-0123456789abcdef"
_BOT_USER = {"id": BOT_ID, "is_bot": True, "first_name": "Finbot", "username": BOT_USERNAME}
_ids = itertools.count(1)


class FakeBotApi(BaseRequest):
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        #: endpoint -> (HTTP status, description) to fail with
        self.failures: dict[str, tuple[int, str]] = {}

    @property
    def read_timeout(self) -> float | None:
        return None

    async def initialize(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    async def do_request(
        self,
        url: str,
        method: str,
        request_data: RequestData | None = None,
        read_timeout: ODVInput[float] = DEFAULT_NONE,
        write_timeout: ODVInput[float] = DEFAULT_NONE,
        connect_timeout: ODVInput[float] = DEFAULT_NONE,
        pool_timeout: ODVInput[float] = DEFAULT_NONE,
    ) -> tuple[int, bytes]:
        endpoint = url.rsplit("/", 1)[-1]
        params = dict(request_data.parameters) if request_data else {}
        self.calls.append((endpoint, params))
        if endpoint in self.failures:
            status, description = self.failures[endpoint]
            body = {"ok": False, "error_code": status, "description": description}
            return status, json.dumps(body).encode()
        result: Any = True
        if endpoint == "getMe":
            result = _BOT_USER
        elif endpoint == "getUpdates":  # an idle long poll: no updates, after a short wait
            await asyncio.sleep(0.01)
            result = []
        elif endpoint == "sendMessage":
            result = {
                "message_id": next(_ids),
                "date": 1_760_000_000,
                "chat": {"id": params["chat_id"], "type": "private"},
                "from": _BOT_USER,
                "text": params.get("text", ""),
            }
        return 200, json.dumps({"ok": True, "result": result}).encode()

    def sent(self, chat_id: int | None = None) -> list[dict[str, Any]]:
        return [
            p
            for endpoint, p in self.calls
            if endpoint == "sendMessage" and (chat_id is None or p["chat_id"] == chat_id)
        ]

    def count(self, endpoint: str) -> int:
        return sum(1 for name, _ in self.calls if name == endpoint)

    def texts(self, chat_id: int | None = None) -> list[str]:
        return [p["text"] for p in self.sent(chat_id)]


def message_update(
    app: Application[Any, Any, Any, Any, Any, Any],
    user_id: int,
    text: str,
    *,
    update_id: int | None = None,
    chat_type: str = "private",
    chat_id: int | None = None,
    first_name: str = "Budi",
    is_bot: bool = False,
) -> Update:
    command = text.split(" ", 1)[0]
    entities = (
        [{"type": "bot_command", "offset": 0, "length": len(command)}]
        if text.startswith("/")
        else []
    )
    data = {
        "update_id": next(_ids) if update_id is None else update_id,
        "message": {
            "message_id": next(_ids),
            "date": 1_760_000_000,
            "chat": {"id": user_id if chat_id is None else chat_id, "type": chat_type},
            "from": {"id": user_id, "is_bot": is_bot, "first_name": first_name},
            "text": text,
            "entities": entities,
        },
    }
    update = Update.de_json(data, app.bot)
    assert update is not None
    return update


def added_to_group_update(
    app: Application[Any, Any, Any, Any, Any, Any], chat_id: int, *, by_user: int = 42
) -> Update:
    data = {
        "update_id": next(_ids),
        "my_chat_member": {
            "chat": {"id": chat_id, "type": "group", "title": "Grup"},
            "from": {"id": by_user, "is_bot": False, "first_name": "Ani"},
            "date": 1_760_000_000,
            "old_chat_member": {"user": _BOT_USER, "status": "left"},
            "new_chat_member": {"user": _BOT_USER, "status": "member"},
        },
    }
    update = Update.de_json(data, app.bot)
    assert update is not None
    return update


async def eventually(condition: Callable[[], bool]) -> None:
    """Wait (up to 10 s) for a condition on state that offers no event to await."""
    async with asyncio.timeout(10):
        while not condition():  # noqa: ASYNC110 - polls fakes and pg_locks, which have no event
            await asyncio.sleep(0.01)
