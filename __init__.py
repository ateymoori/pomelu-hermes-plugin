"""Pomelu plugin: messages Hermes starts on its own reach the Pomelu app.

Pomelu talks to each bot in its Bot Chat as a ``gui`` session. Such a session
has no messaging origin, so a cron job made there (a reminder, "every day at
7") went to a Telegram home channel or nowhere. This plugin routes those jobs
back into the Bot Chat (``deliver='bot-chat'``), where Pomelu notifies the
user, and tells the agent how to reach the user later.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

APP_SOURCE = "gui"
CRON_TOOL = "cronjob_manage"
BOT_CHAT = "bot-chat"
ORIGIN = "origin"

PROMPT = (
    "The user talks to you in the Pomelu app, in this chat. Pomelu shows a "
    "notification when a message lands in this chat, also while the app is "
    "closed. To message the user later or on a schedule (a reminder, \"every "
    "day at 7\", a report when a long task ends), create a cron job: it is "
    "delivered into this chat (deliver='bot-chat'). The send_message tool "
    "cannot reach this chat. Deliver to another platform only when the user "
    "asks for it."
)


def route_deliver(deliver: Any) -> str | None:
    """The ``deliver`` value for a job made in the app, or None to keep it.

    No value and ``origin`` mean "back here": the Bot Chat. A target the agent
    or the user chose (``telegram``, ``local``, ``all``) stays.
    """
    parts = [p.strip() for p in str(deliver or "").split(",") if p.strip()]
    if not parts:
        return BOT_CHAT
    if ORIGIN not in (p.lower() for p in parts):
        return None
    routed = [BOT_CHAT if p.lower() == ORIGIN else p for p in parts]
    return ",".join(dict.fromkeys(routed))


def session_source(session_id: Any) -> str:
    """The stored ``source`` of a session in the active profile, or ``""``."""
    if not session_id:
        return ""
    try:
        from hermes_constants import get_hermes_home
        from hermes_state import SessionDB

        db_path = get_hermes_home() / "state.db"
        if not db_path.is_file():
            return ""
        db = SessionDB(db_path=db_path, read_only=True)
        try:
            row = db.get_session_list_row(str(session_id))
        finally:
            db.close()
    except Exception:
        logger.warning("pomelu: cannot read session %s", session_id, exc_info=True)
        return ""
    return str((row or {}).get("source") or "")


def _pre_tool_call(tool_name: str = "", args: Any = None, session_id: str = "", **_: Any):
    if tool_name != CRON_TOOL or not isinstance(args, dict):
        return None
    if str(args.get("action") or "").strip().lower() != "create":
        return None
    deliver = route_deliver(args.get("deliver"))
    if deliver is None or session_source(session_id) != APP_SOURCE:
        return None
    logger.info("pomelu: cron job from app session %s delivers to %s", session_id, deliver)
    return {"action": "modify", "args": {"deliver": deliver}}


def _prompt(info: Any) -> str:
    return PROMPT if session_source(info.get("session_id")) == APP_SOURCE else ""


def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", _pre_tool_call)
    ctx.register_system_prompt_section("pomelu.delivery", _prompt)
