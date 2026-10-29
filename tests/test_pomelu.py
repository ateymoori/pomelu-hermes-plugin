"""Behavior of the Pomelu plugin's cron routing, through Hermes' own tool dispatch.

Run with the Hermes venv, from a Hermes checkout or install:
``HERMES_HOME=$(mktemp -d) <hermes>/venv/bin/python -m pytest tests``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

# The repo folder is not an importable package name, so load the plugin by path.
_spec = importlib.util.spec_from_file_location(
    "pomelu_plugin", Path(__file__).resolve().parents[1] / "__init__.py")
pomelu = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pomelu)


@pytest.mark.parametrize(
    ("deliver", "routed"),
    [
        (None, "bot-chat"),
        ("", "bot-chat"),
        ("origin", "bot-chat"),
        ("origin,telegram", "bot-chat,telegram"),
        ("telegram", None),
        ("local", None),
        ("bot-chat:research", None),
    ],
)
def test_route_deliver(deliver, routed):
    assert pomelu.route_deliver(deliver) == routed


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("app", source="gui", model="m")
    db.create_session("tg", source="telegram", model="m")
    db.close()
    return tmp_path


def _create(home, session_id, **extra):
    args = {"action": "create", "schedule": "0 7 * * *", "prompt": "Say good morning.", **extra}
    return pomelu._pre_tool_call(tool_name="cronjob_manage", args=args, session_id=session_id)


def test_job_made_in_the_app_goes_to_the_bot_chat(home):
    assert _create(home, "app") == {"action": "modify", "args": {"deliver": "bot-chat"}}


def test_a_chosen_target_stays(home):
    assert _create(home, "app", deliver="telegram") is None


def test_jobs_from_other_platforms_stay(home):
    assert _create(home, "tg") is None


def test_only_create_is_routed(home):
    args = {"action": "update", "job_id": "x"}
    assert pomelu._pre_tool_call(tool_name="cronjob_manage", args=args, session_id="app") is None


def test_prompt_only_in_app_sessions(home):
    assert "Pomelu" in pomelu._prompt({"session_id": "app"})
    assert pomelu._prompt({"session_id": "tg"}) == ""
