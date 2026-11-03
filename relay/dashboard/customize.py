"""UI preferences and the bot's Discord profile."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from aiohttp import web

from relay.dashboard import bridge_client as bc

THEMES = ("midnight", "violet", "orchid", "ember", "emerald", "ice", "mono", "rainbow", "custom")
SURFACES = ("charcoal", "graphite", "steel", "slate", "stone")
PREFS_VERSION = 2  # version 1 saved the old default surface explicitly, so it is reset once
DEFAULT_PREFS = {"theme": "midnight", "accent": None, "surface": "charcoal", "guild": None}
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
AVATAR_RE = re.compile(r"^data:image/(png|jpeg|gif|webp);base64,[A-Za-z0-9+/=\s]+$")
MAX_AVATAR_B64 = 8 * 1024 * 1024


def clean_prefs(raw: dict, cur: dict) -> dict:
    out = dict(DEFAULT_PREFS)
    out.update({k: cur.get(k, v) for k, v in DEFAULT_PREFS.items()})
    if raw.get("theme") in THEMES:
        out["theme"] = raw["theme"]
    if raw.get("surface") in SURFACES:
        out["surface"] = raw["surface"]
    if "accent" in raw:
        a = raw["accent"]
        out["accent"] = a.lower() if isinstance(a, str) and HEX.match(a) else (None if a is None else out["accent"])
    if "guild" in raw:
        g = str(raw["guild"] or "")
        out["guild"] = g if g.isdigit() and len(g) < 25 else None
    return out


def _prefs_path(root) -> Path:
    return Path(root) / "data" / "ui_prefs.json"


def load_prefs(root) -> dict:
    try:
        cur = json.loads(_prefs_path(root).read_text(encoding="utf-8"))
        cur = cur if isinstance(cur, dict) else {}
        if cur.get("v") != PREFS_VERSION:
            cur.pop("surface", None)
        return clean_prefs(cur, {})
    except (OSError, ValueError):
        return dict(DEFAULT_PREFS)


def save_prefs(root, prefs: dict) -> None:
    p = _prefs_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps({**prefs, "v": PREFS_VERSION}), encoding="utf-8")
    os.replace(tmp, p)


def _fail(msg: str, status: int = 400) -> web.Response:
    return web.json_response({"ok": False, "error": msg}, status=status)


def _avatar_file(root) -> Path | None:
    for ext in ("png", "jpg", "gif", "webp"):
        p = Path(root) / "data" / f"avatar.{ext}"
        if p.exists():
            return p
    return None


async def get_prefs(request: web.Request):
    return web.json_response({"ok": True, "prefs": load_prefs(request.app["root"])})


async def post_prefs(request: web.Request):
    root = request.app["root"]
    body = await request.json()
    if not isinstance(body, dict):
        return _fail("bad request")
    prefs = clean_prefs(body, load_prefs(root))
    save_prefs(root, prefs)
    return web.json_response({"ok": True, "prefs": prefs})


async def get_profile(request: web.Request):
    from relay.dashboard import server as S
    app, root = request.app, request.app["root"]
    _, cfg = S.readiness(root)
    raw = S.load_raw_config(root).get("presence", {})
    try:
        from relay.config import PresenceCfg
        pres = PresenceCfg.model_validate(raw).model_dump()
    except Exception:
        pres = cfg.presence.model_dump() if cfg is not None else {}
    return web.json_response({"ok": True, "presence": pres, "live": bc.read_state(root),
                              "online": app["bot"].status()["state"] == "online", "has_avatar": _avatar_file(root) is not None})


def _need_online(app) -> web.Response | None:
    if app["bot"].status()["state"] != "online" or bc.read_state(app["root"]) is None:
        return _fail("Relay has to be online for that. Start it from the Home tab.", 409)
    return None


async def post_presence(request: web.Request):
    from relay.dashboard import server as S
    from relay.config import Config, PresenceCfg
    from pydantic import ValidationError
    app, root = request.app, request.app["root"]
    body = await request.json()
    try:
        pres = PresenceCfg.model_validate(body.get("presence") or {}).model_dump()
        merged = S.deep_merge(S.load_raw_config(root), {"presence": pres})
        Config.model_validate(merged)
    except (ValidationError, ValueError) as e:
        return _fail("That status isn't valid: " + str(e).splitlines()[-1][:140], 422)
    S.save_config(root, merged)
    applied, note = False, "Saved. It will show next time Relay starts."
    if app["bot"].status()["state"] == "online" and bc.read_state(root) is not None:
        res = await bc.call(root, {"type": "presence", "presence": pres}, timeout=20)
        applied = bool(res.get("ok"))
        note = "Saved and live in Discord." if applied else "Saved, but I couldn't update Discord right now: " + str(res.get("error", ""))
    return web.json_response({"ok": True, "applied": applied, "note": note, "presence": pres})


async def post_nickname(request: web.Request):
    app, root = request.app, request.app["root"]
    if (bad := _need_online(app)) is not None:
        return bad
    body = await request.json()
    gid = str(body.get("guild_id") or "")
    if not gid.isdigit():
        return _fail("Pick a server first.")
    res = await bc.call(root, {"type": "nickname", "guild_id": gid, "nickname": str(body.get("nickname") or "")[:40]}, timeout=30)
    return web.json_response(res, status=200 if res.get("ok") else 400)


async def post_username(request: web.Request):
    app, root = request.app, request.app["root"]
    if (bad := _need_online(app)) is not None:
        return bad
    body = await request.json()
    res = await bc.call(root, {"type": "username", "name": str(body.get("name") or "")[:40]}, timeout=30)
    return web.json_response(res, status=200 if res.get("ok") else 400)


async def post_avatar(request: web.Request):
    app, root = request.app, request.app["root"]
    if (bad := _need_online(app)) is not None:
        return bad
    body = await request.json()
    data = str(body.get("data") or "")
    if len(data) > MAX_AVATAR_B64 or not AVATAR_RE.match(data):
        return _fail("Use a PNG, JPG, GIF or WebP picture under 6 MB.")
    res = await bc.call(root, {"type": "avatar", "data": data}, timeout=60)
    if res.get("ok"):
        import base64
        ext = {"jpeg": "jpg"}.get(data[11:data.index(";")], data[11:data.index(";")])
        for old in Path(root, "data").glob("avatar.*"):
            old.unlink(missing_ok=True)
        Path(root, "data", f"avatar.{ext}").write_bytes(base64.b64decode(data.split(",", 1)[1]))
    return web.json_response(res, status=200 if res.get("ok") else 400)


async def get_avatar(request: web.Request):
    p = _avatar_file(request.app["root"])
    if p is None:
        return _fail("no avatar saved", 404)
    return web.FileResponse(p)


def add_routes(app: web.Application) -> None:
    r = app.router
    r.add_get("/api/prefs", get_prefs)
    r.add_post("/api/prefs", post_prefs)
    r.add_get("/api/profile", get_profile)
    r.add_post("/api/profile/presence", post_presence)
    r.add_post("/api/profile/nickname", post_nickname)
    r.add_post("/api/profile/username", post_username)
    r.add_post("/api/profile/avatar", post_avatar)
    r.add_get("/api/avatar", get_avatar)
