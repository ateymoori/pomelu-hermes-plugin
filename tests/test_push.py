"""Push behavior: which replies ring a phone, what the phone can open, and the
routes the app calls. Same run as test_pomelu.py."""

from __future__ import annotations

import base64
import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_root = Path(__file__).resolve().parents[1]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


push = _load("pomelu_push", _root / "push.py")
KEY = base64.b64encode(bytes(range(32))).decode()
IOS_TOKEN = "ab" * 32


class _Relay(BaseHTTPRequestHandler):
    status = 200
    received: list = []

    def do_POST(self):
        self.received.append(json.loads(self.rfile.read(int(self.headers["content-length"]))))
        self.send_response(self.status)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *_):
        pass


@pytest.fixture
def relay(monkeypatch):
    _Relay.status, _Relay.received = 200, []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Relay)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(push, "RELAY_URL", f"http://127.0.0.1:{server.server_port}/v1/push")
    # Sending normally runs on its own thread; here it runs before on_reply returns.
    monkeypatch.setattr(push.threading, "Thread",
                        lambda target, args, **_: type("T", (), {"start": lambda self: target(*args)})())
    yield _Relay
    server.shutdown()


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("chat", source="gui", model="m")
    db.set_session_title("chat", "Bot Chat")
    db.create_session("other", source="gui", model="m")
    db.close()
    (tmp_path / "profile.yaml").write_text("ui_meta:\n  hermes-bots:\n    title: Atlas\n")
    return tmp_path


def _phone(device_id="phone-0001", visible=False, **extra):
    push.register_device(device_id, {"platform": "ios", "token": IOS_TOKEN, "key": KEY, **extra})
    push.set_presence(device_id, visible)


def _opened(entry):
    raw = base64.b64decode(entry["data"])
    return json.loads(AESGCM(base64.b64decode(KEY)).decrypt(raw[:12], raw[12:], None))


def test_a_bot_chat_reply_rings_an_away_phone_sealed_with_its_key(home, relay):
    _phone()
    push.on_reply(session_id="chat", assistant_response="Time to stretch!\nMEDIA:/tmp/a.png")
    [sent] = relay.received
    assert (sent["platform"], sent["token"], sent["sandbox"]) == ("ios", IOS_TOKEN, False)
    assert "stretch" not in sent["data"]
    assert _opened(sent) == {"v": 1, "bot": "default", "title": "Atlas", "body": "Time to stretch!"}


def test_no_push_while_the_app_is_open(home, relay):
    _phone(visible=True)
    push.on_reply(session_id="chat", assistant_response="hi")
    assert relay.received == []


def test_an_open_app_that_went_quiet_counts_as_away(home, relay, monkeypatch):
    _phone(visible=True)
    later = push.time.time() + push.STALE_VISIBLE_S + 1
    monkeypatch.setattr(push.time, "time", lambda: later)
    push.on_reply(session_id="chat", assistant_response="hi")
    assert len(relay.received) == 1


def test_replies_outside_the_bot_chat_stay_quiet(home, relay):
    _phone()
    push.on_reply(session_id="other", assistant_response="hi")
    assert relay.received == []


def test_a_phone_the_relay_calls_gone_is_forgotten(home, relay):
    _phone()
    relay.status = 410
    push.on_reply(session_id="chat", assistant_response="hi")
    assert push.load_devices() == {}


def test_a_reinstalled_app_replaces_its_old_entry(home):
    _phone("phone-0001")
    _phone("phone-0002")
    assert list(push.load_devices()) == ["phone-0002"]


@pytest.mark.parametrize(
    "body",
    [
        {"platform": "web", "token": IOS_TOKEN, "key": KEY},
        {"platform": "ios", "token": "nope", "key": KEY},
        {"platform": "ios", "token": IOS_TOKEN, "key": base64.b64encode(b"short").decode()},
        {"platform": "ios", "token": IOS_TOKEN, "key": KEY, "sandbox": "yes"},
    ],
)
def test_a_malformed_phone_is_refused(home, body):
    with pytest.raises(ValueError):
        push.register_device("phone-0001", body)


def test_the_preview_drops_file_lines_and_cuts_long_text():
    assert push.preview("MEDIA:/tmp/x.png") == "\U0001f4ce"
    long = push.preview("word " * 100)
    assert len(long) <= push.PREVIEW_CHARS + 1 and long.endswith("…")


def test_setup_turns_the_plugin_on_for_bots_that_lack_it(home, monkeypatch):
    for name, plugins in [("atlas", "enabled: []"), ("muse", "enabled: [pomelu]"), ("quiet", "disabled: [pomelu]")]:
        (home / "profiles" / name).mkdir(parents=True)
        (home / "profiles" / name / "config.yaml").write_text(f"plugins:\n  {plugins}\n")
    calls = []
    monkeypatch.setattr(push.subprocess, "run",
                        lambda argv, **_: calls.append(argv) or type("R", (), {"returncode": 0})())
    assert push.enable_everywhere() == ["atlas"]
    assert calls[0][-5:-1] == ["atlas", "plugins", "enable", "pomelu"]


def test_the_app_routes(home, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    api = _load("pomelu_api", _root / "dashboard" / "plugin_api.py")
    monkeypatch.setattr(push, "enable_everywhere", lambda: [])
    import sys

    monkeypatch.setitem(sys.modules, "pomelu_push", push)
    app = FastAPI()
    app.include_router(api.router)
    client = TestClient(app)
    assert client.get("/status").json()["push"] is True
    phone = {"platform": "ios", "token": IOS_TOKEN, "key": KEY, "sandbox": True}
    assert client.put("/devices/phone-0001", json=phone).status_code == 200
    assert client.put("/devices/phone-0001", json={**phone, "key": "x"}).status_code == 400
    assert client.put("/devices/phone-0001/presence", json={"visible": False}).status_code == 200
    assert client.put("/devices/unknown-01/presence", json={"visible": False}).status_code == 404
    assert push.load_devices()["phone-0001"]["visible"] is False
    assert client.delete("/devices/phone-0001").status_code == 200
    assert push.load_devices() == {}
