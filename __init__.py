"""Pomelu plugin: messages Hermes starts on its own reach the Pomelu app.

Pomelu talks to each bot in its Bot Chat as a ``gui`` session. Such a session
has no messaging origin, so a cron job made there (a reminder, "every day at
7") went to a Telegram home channel or nowhere. This plugin routes those jobs
back into the Bot Chat (``deliver='bot-chat'``) and tells the agent how to
reach the user later. A reply in a Bot Chat then rings the phone through
push (``push.py``), also while the app is closed.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
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
    "asks for it. When the user leaves the app, this chat is released: "
    "background processes and async subagents you started here stop. For work "
    "that runs on after your reply, run it detached and add a cron job that "
    "checks it and reports here; never promise a later update without one."
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


def load_push():
    """``push.py``, shared with the dashboard routes (one module per process)."""
    name = "pomelu_push"
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name("push.py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return module


def _on_reply(**kwargs: Any) -> None:
    load_push().on_reply(**kwargs)


def _cli_setup(parser) -> None:
    sub = parser.add_subparsers(dest="pomelu_command")
    sub.add_parser("setup", help="Turn the plugin on for every bot (profile)")
    sub.add_parser("status", help="Show the phones that get push messages")


def _cli(args) -> None:
    push = load_push()
    if getattr(args, "pomelu_command", None) == "setup":
        done = push.enable_everywhere()
        missing = push.profiles_without_plugin()
        if done:
            print(f"Turned on for: {', '.join(done)}")
        if missing:
            print(f"Could not turn on for: {', '.join(missing)} (see agent.log)")
            raise SystemExit(1)
        if not done:
            print("Every bot already has the Pomelu plugin.")
        return
    devices = push.load_devices()
    print(f"{len(devices)} phone(s) get push messages; relay {push.RELAY_URL}")
    for device_id, d in devices.items():
        print(f"  {device_id}  {d.get('platform')}  {'open' if d.get('visible') else 'away'}")
    missing = push.profiles_without_plugin()
    if missing:
        print(f"Bots without the plugin: {', '.join(missing)} (run: hermes pomelu setup)")


def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", _pre_tool_call)
    ctx.register_hook("post_llm_call", _on_reply)
    ctx.register_system_prompt_section("pomelu.delivery", _prompt)
    ctx.register_cli_command("pomelu", "Pomelu app: push messages to phones", _cli_setup, _cli)
