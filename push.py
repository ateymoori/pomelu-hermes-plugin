"""Push: a bot's reply in its Bot Chat reaches the phone while Pomelu is away.

The app registers each phone here (``dashboard/plugin_api.py``): its push
token and a key of its own. When a bot answers in a Bot Chat and no phone has
the app open, the reply is sealed with each phone's key (AES-256-GCM) and
sent through the Pomelu push relay to Apple or Google. Only the phone can
open it; the relay holds the app's push keys and stores nothing.

Phones are kept for the whole Hermes install (the root home), not per
profile: one phone gets every bot's messages.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import os
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PLUGIN = "pomelu"
_enabling = threading.Lock()
RELAY_URL = os.environ.get("POMELU_PUSH_URL", "https://pomelu-push.teymoori-net.workers.dev/v1/push")
# The canonical chat Pomelu shows for a bot (and its compacted parts).
BOT_CHAT_TITLE = re.compile(r"^Bot Chat( #\d+)?$")
# A phone that said "open" and went quiet this long ago is away.
STALE_VISIBLE_S = 3 * 60
RETRY_AFTER_S = 2
PREVIEW_CHARS = 240
MEDIA_PREFIX = "MEDIA:"
RELAY_TIMEOUT_S = 15

_DEVICE_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")
_TOKENS = {
    "ios": re.compile(r"^[0-9a-fA-F]{64,200}$"),
    "android": re.compile(r"^[\w:-]{32,400}$"),
}
_lock = threading.Lock()


# --------------------------------------------------------------- devices


def store_path() -> Path:
    from hermes_constants import get_default_hermes_root

    return get_default_hermes_root() / "pomelu" / "devices.json"


def load_devices() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(store_path().read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        logger.warning("pomelu: unreadable device list, starting empty", exc_info=True)
        return {}
    devices = data.get("devices") if isinstance(data, dict) else None
    return devices if isinstance(devices, dict) else {}


def _save(devices: dict[str, dict[str, Any]]) -> None:
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".devices-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump({"devices": devices}, f)
        os.chmod(tmp, 0o600)  # the phones' keys
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def register_device(device_id: str, body: Any) -> None:
    """Adds or renews a phone; raises ValueError on a malformed one."""
    if not _DEVICE_ID.match(str(device_id)):
        raise ValueError("bad device id")
    if not isinstance(body, dict):
        raise ValueError("body must be an object")
    platform, token, key = body.get("platform"), body.get("token"), body.get("key")
    sandbox = body.get("sandbox", False)
    if platform not in _TOKENS:
        raise ValueError("platform must be ios or android")
    if not isinstance(token, str) or not _TOKENS[platform].match(token):
        raise ValueError("bad push token")
    try:
        raw_key = base64.b64decode(str(key), validate=True)
    except (binascii.Error, ValueError):
        raw_key = b""
    if len(raw_key) != 32:
        raise ValueError("key must be 32 bytes, base64")
    if not isinstance(sandbox, bool):
        raise ValueError("sandbox must be true or false")
    now = time.time()
    with _lock:
        devices = load_devices()
        # One token, one phone: a reinstall must not ring twice.
        for other in [i for i, d in devices.items() if d.get("token") == token and i != device_id]:
            devices.pop(other)
        devices[device_id] = {
            "platform": platform,
            "token": token,
            "key": key,
            "sandbox": sandbox,
            "visible": True,
            "seen": now,
        }
        _save(devices)
    logger.info("pomelu: phone %s registered for push (%s)", device_id, platform)


def remove_device(device_id: str) -> bool:
    with _lock:
        devices = load_devices()
        if devices.pop(device_id, None) is None:
            return False
        _save(devices)
    return True


def set_presence(device_id: str, visible: bool) -> bool:
    """The app is open (or not) on this phone; False for an unknown phone."""
    with _lock:
        devices = load_devices()
        device = devices.get(device_id)
        if device is None:
            return False
        device.update(visible=bool(visible), seen=time.time())
        _save(devices)
    return True


def is_away(device: dict[str, Any], now: float) -> bool:
    return not device.get("visible") or now - float(device.get("seen") or 0) > STALE_VISIBLE_S


# --------------------------------------------------------------- message


def preview(text: Any) -> str:
    """What the notification says: the words, cut; a paperclip for files only."""
    lines = [ln for ln in str(text or "").splitlines() if not ln.lstrip().startswith(MEDIA_PREFIX)]
    flat = " ".join(" ".join(lines).split())
    if not flat:
        return "\U0001f4ce"
    return flat if len(flat) <= PREVIEW_CHARS else flat[:PREVIEW_CHARS].rstrip() + "…"


def seal(key_b64: str, message: dict[str, Any]) -> str:
    """``base64(nonce || ciphertext || tag)``, as CryptoKit and javax.crypto read it."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = secrets.token_bytes(12)
    sealed = AESGCM(base64.b64decode(key_b64)).encrypt(
        nonce, json.dumps(message, ensure_ascii=False).encode(), None)
    return base64.b64encode(nonce + sealed).decode()


def collapse_id(key_b64: str, profile: str) -> str:
    """Opaque per phone and bot: Apple replaces the bot's last row, and the
    relay learns nothing about the bot."""
    return hashlib.sha256(f"{key_b64}/{profile}".encode()).hexdigest()[:32]


def bot_title(home: Path, profile: str) -> str:
    """The name Pomelu shows for the bot: its Bot Mode title, else its id."""
    try:
        data = _yaml_load((home / "profile.yaml").read_text()) or {}
    except Exception:  # missing or broken: the profile's, not ours
        return profile
    meta = data.get("ui_meta") if isinstance(data, dict) else None
    bots = meta.get("hermes-bots") if isinstance(meta, dict) else None
    title = bots.get("title") if isinstance(bots, dict) else None
    shown = str(title or (data.get("display_name") if isinstance(data, dict) else "") or "").strip()
    return shown or profile


# --------------------------------------------------------------- the hook


def _session_row(home: Path, session_id: str) -> dict[str, Any] | None:
    from hermes_state import SessionDB

    db_path = home / "state.db"
    if not db_path.is_file():
        return None
    db = SessionDB(db_path=db_path, read_only=True)
    try:
        return db.get_session_list_row(session_id)
    finally:
        db.close()


def on_reply(session_id: str = "", assistant_response: Any = None, **_: Any) -> None:
    """``post_llm_call``: a finished reply. A Bot Chat's goes to each phone
    without the app open. Sending runs off the agent's thread."""
    if not session_id or not str(assistant_response or "").strip():
        return
    devices = {i: d for i, d in load_devices().items() if is_away(d, time.time())}
    if not devices:
        return
    try:
        from hermes_constants import get_hermes_home, profile_name_for_home

        home = get_hermes_home()
        row = _session_row(home, str(session_id))
        profile = profile_name_for_home(home) or "default"
    except Exception:
        logger.warning("pomelu: cannot read session %s", session_id, exc_info=True)
        return
    if not row or not BOT_CHAT_TITLE.match(str(row.get("title") or "")):
        return
    message = {"v": 1, "bot": profile, "title": bot_title(home, profile), "body": preview(assistant_response)}
    threading.Thread(target=send_all, args=(devices, message, profile), daemon=True,
                     name="pomelu-push").start()


def send_all(devices: dict[str, dict[str, Any]], message: dict[str, Any], profile: str) -> None:
    for device_id, device in devices.items():
        try:
            status = send(device, message, profile)
            if status == 0 or status >= 500:
                time.sleep(RETRY_AFTER_S)
                status = send(device, message, profile)
        except KeyError:
            logger.warning("pomelu: phone %s has a broken entry", device_id)
            continue
        if status == 410:
            logger.info("pomelu: phone %s is gone (app removed or token renewed); forgetting it", device_id)
            remove_device(device_id)


def send(device: dict[str, Any], message: dict[str, Any], profile: str) -> int:
    """One push through the relay; its HTTP status (0: not reached)."""
    body = {
        "platform": device["platform"],
        "token": device["token"],
        "sandbox": bool(device.get("sandbox")),
        "data": seal(device["key"], message),
        "collapse": collapse_id(device["key"], profile),
    }
    request = urllib.request.Request(
        RELAY_URL, data=json.dumps(body).encode(), method="POST",
        headers={"content-type": "application/json", "user-agent": "pomelu-hermes-plugin"})
    try:
        with urllib.request.urlopen(request, timeout=RELAY_TIMEOUT_S) as response:
            return response.status
    except urllib.error.HTTPError as e:
        if e.code != 410:
            logger.warning("pomelu: push relay answered %s: %s", e.code, e.read()[:200])
        return e.code
    except OSError as e:
        logger.warning("pomelu: push relay not reached: %s", e)
        return 0


# --------------------------------------------------------------- profiles


def _yaml_load(text: str) -> Any:
    try:
        from hermes_yaml import safe_load  # Hermes' own reader of its YAML files
    except ImportError:
        from yaml import safe_load
    return safe_load(text)


def profiles_without_plugin() -> list[str]:
    """Bots (profiles) that have not turned the plugin on, nor off on purpose."""
    from hermes_constants import get_default_hermes_root

    missing = []
    for config in sorted((get_default_hermes_root() / "profiles").glob("*/config.yaml")):
        try:
            plugins = (_yaml_load(config.read_text()) or {}).get("plugins") or {}
        except Exception:
            logger.warning("pomelu: unreadable %s", config)
            continue
        enabled, disabled = plugins.get("enabled") or [], plugins.get("disabled") or []
        if PLUGIN not in enabled and PLUGIN not in disabled:
            missing.append(config.parent.name)
    return missing


def _link_plugin(plugins_dir: Path) -> None:
    """A profile only sees the plugins in its own folder, and the install puts
    this one in the root's: Hermes cannot turn it on there without a link."""
    link = plugins_dir / PLUGIN
    if link.exists() or link.is_symlink():
        return
    plugins_dir.mkdir(parents=True, exist_ok=True)
    link.symlink_to(Path(__file__).resolve().parent, target_is_directory=True)


def enable_everywhere_soon() -> None:
    """Off the caller's thread, one run at a time: one Hermes command per bot
    can take minutes, and a phone must not wait for it."""
    if not _enabling.acquire(blocking=False):
        return

    def run() -> None:
        try:
            enable_everywhere()
        except Exception:
            logger.warning("pomelu: could not turn the plugin on for every bot", exc_info=True)
        finally:
            _enabling.release()

    threading.Thread(target=run, daemon=True, name="pomelu-enable").start()


def enable_everywhere() -> list[str]:
    """Turns the plugin on for every bot that lacks it, with Hermes' own
    command; a bot that turned it off stays off. Returns the bots turned on."""
    from hermes_constants import get_default_hermes_root

    done = []
    for profile in profiles_without_plugin():
        _link_plugin(get_default_hermes_root() / "profiles" / profile / "plugins")
        result = subprocess.run(
            [sys.executable, "-m", "hermes_cli.main", "-p", profile, "plugins", "enable", PLUGIN,
             "--no-allow-tool-override"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)
        if result.returncode == 0:
            done.append(profile)
            logger.info("pomelu: turned on for bot %s", profile)
        else:
            # Hermes prints why on stdout.
            reason = (result.stderr or result.stdout or "").strip()[-300:]
            logger.warning("pomelu: could not turn on for bot %s: %s", profile, reason)
    return done
