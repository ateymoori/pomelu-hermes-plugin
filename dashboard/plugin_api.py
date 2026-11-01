"""Routes the Pomelu app calls, under ``/api/plugins/pomelu/`` (the
dashboard's own sign-in guards them).

- ``GET    /status``: the plugin's version, so the app knows push works.
- ``PUT    /devices/{id}``: a phone's push token and key (``push.register_device``).
- ``DELETE /devices/{id}``: the phone turned notifications off.
- ``PUT    /devices/{id}/presence``: ``{"visible": bool}``, the app opened or left.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException

VERSION = "1.1.2"
router = APIRouter()


def _push():
    name = "pomelu_push"
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parents[1] / "push.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return module


@router.get("/status")
async def status():
    return {"version": VERSION, "push": True}


@router.put("/devices/{device_id}")
async def put_device(device_id: str, body: dict = Body(...)):
    push = _push()
    try:
        await asyncio.to_thread(push.register_device, device_id, body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    # A bot made later (from the app too) gets the plugin on the phone's next visit.
    push.enable_everywhere_soon()
    return {"ok": True, "version": VERSION}


@router.delete("/devices/{device_id}")
async def delete_device(device_id: str):
    await asyncio.to_thread(_push().remove_device, device_id)
    return {"ok": True}


@router.put("/devices/{device_id}/presence")
async def put_presence(device_id: str, body: dict = Body(...)):
    if not await asyncio.to_thread(_push().set_presence, device_id, bool(body.get("visible"))):
        raise HTTPException(status_code=404, detail="unknown device")
    return {"ok": True}
