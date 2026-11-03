from __future__ import annotations

import base64
import copy
import hmac
import json
import os
import secrets
import shutil
from pathlib import Path

import discord
from aiohttp import web
from pydantic import ValidationError

from relay.ai.prompts import personality_block
from relay.config import Config, PersonalityCfg
from relay.dashboard import bridge_client
from relay.dashboard.chat import add_routes as add_chat_routes
from relay.dashboard.customize import add_routes as add_custom_routes
from relay.dashboard.envfile import mask, read_env, write_env
from relay.dashboard.process import BotProcess
from relay.logging_setup import scrub

ROOT = Path(__file__).resolve().parents[2]
STATIC = Path(__file__).resolve().parent / "static"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}

ENV_VARS = [
    {"key": "DISCORD_TOKEN", "label": "Discord bot token", "secret": True, "need": "always",
     "help": "Developer Portal → your application → Bot → Reset Token."},
    {"key": "GROQ_API_KEY", "label": "Groq API key", "secret": True, "need": "always",
     "help": "console.groq.com → API Keys."},
    {"key": "TURSO_DATABASE_URL", "label": "Turso database URL", "secret": False, "need": "turso",
     "help": "Only used when storage mode is turso. Looks like libsql://name-org.turso.io"},
    {"key": "TURSO_AUTH_TOKEN", "label": "Turso auth token", "secret": True, "need": "turso",
     "help": "turso db tokens create <db-name>"},
    {"key": "AUDIT_HMAC_KEY", "label": "Audit signing key", "secret": True, "need": "never", "generate": True,
     "help": "Optional. Makes the audit log tamper-evident. Set it before logging starts; changing it later breaks verification of older entries."},
]
ENV_KEYS = {v["key"] for v in ENV_VARS}
INVITE_PERMS = discord.Permissions(view_channel=True, send_messages=True, read_message_history=True, manage_channels=True,
                                   manage_roles=True, moderate_members=True, kick_members=True, ban_members=True,
                                   manage_nicknames=True, manage_messages=True, view_audit_log=True, add_reactions=True)
LOG_FILES = {"bot": "relay.log", "console": "console.log"}
MAX_CHUNK = 256 * 1024


def _err(msg: str, status: int = 400, **extra) -> web.Response:
    return web.json_response({"ok": False, "error": msg, **extra}, status=status)


def _fmt_errors(e: ValidationError) -> list[dict]:
    return [{"path": ".".join(str(x) for x in err["loc"]), "msg": err["msg"]} for err in e.errors()]


def _config_path(root: Path) -> Path:
    return root / "config.json"


def load_raw_config(root: Path) -> dict:
    return json.loads(_config_path(root).read_text(encoding="utf-8"))


def save_config(root: Path, data: dict) -> None:
    path = _config_path(root)
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def deep_merge(base: dict, patch: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def env_values(root: Path) -> dict[str, str]:
    file_vals = read_env(root / ".env")
    return {k: (os.environ.get(k) or file_vals.get(k) or "") for k in ENV_KEYS}


def readiness(root: Path) -> tuple[list[dict], Config | None]:
    checks, cfg = [], None
    try:
        cfg = Config.model_validate(load_raw_config(root))
        checks.append({"id": "config", "label": "Configuration is valid", "ok": True, "tab": "config"})
    except FileNotFoundError:
        checks.append({"id": "config", "label": "config.json is missing", "ok": False, "tab": "config"})
    except json.JSONDecodeError as e:
        checks.append({"id": "config", "label": "config.json isn't valid JSON", "ok": False, "detail": str(e)[:120], "tab": "config"})
    except ValidationError as e:
        first = _fmt_errors(e)[0]
        checks.append({"id": "config", "label": "Configuration has errors", "ok": False, "detail": f"{first['path']}: {first['msg']}", "tab": "config"})
    vals = env_values(root)
    for v in ENV_VARS:
        needed = v["need"] == "always" or (v["need"] == "turso" and cfg is not None and cfg.storage.mode == "turso")
        if not needed:
            continue
        ok = bool(vals[v["key"]].strip())
        checks.append({"id": v["key"], "label": f"{v['label']} is set" if ok else f"{v['label']} is missing", "ok": ok, "tab": "env"})
    return checks, cfg


def invite_url(token: str) -> str | None:
    try:
        head = token.split(".")[0]
        cid = base64.urlsafe_b64decode(head + "=" * (-len(head) % 4)).decode()
        if not cid.isdigit():
            return None
    except Exception:
        return None
    return f"https://discord.com/oauth2/authorize?client_id={cid}&scope=bot+applications.commands&permissions={INVITE_PERMS.value}"


@web.middleware
async def guard(request: web.Request, handler):
    host = request.host.rsplit(":", 1)[0].strip("[]").lower() if request.host else ""
    if host not in LOCAL_HOSTS:
        return web.Response(status=403, text="forbidden host")
    if request.path.startswith("/api/"):
        given = request.headers.get("X-Relay-Token", "")
        if not hmac.compare_digest(given.encode(), request.app["token"].encode()):
            return _err("unauthorized", 401)
        origin = request.headers.get("Origin")
        if request.method != "GET" and origin and origin != f"{request.scheme}://{request.host}":
            return _err("bad origin", 403)
    resp = await handler(request)
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Content-Security-Policy"] = ("default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
                                               "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
    return resp


async def index(request: web.Request):
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    shell = request.app.get("shell")
    if shell in ("mac", "native"):
        html = html.replace('<html lang="en">', f'<html lang="en" data-shell="{shell}">', 1)
    return web.Response(text=html, content_type="text/html")


async def status(request: web.Request):
    root, bot = request.app["root"], request.app["bot"]
    checks, cfg = readiness(root)
    b = bot.status()
    ready = all(c["ok"] for c in checks)
    vals = env_values(root)
    info = {}
    if cfg:
        info = {"name": cfg.bot.name, "model": cfg.groq.model, "storage": cfg.storage.mode, "prefix": cfg.bot.prefix,
                "timezone": cfg.timezone}
    live = bridge_client.read_state(root) if b["state"] == "online" else None
    summary = {"servers": len(live["guilds"]), "members": sum(g.get("members", 0) for g in live["guilds"]),
               "latency_ms": live.get("latency_ms"), "user": (live.get("user") or {}).get("name"),
               "guilds": [{"name": g["name"], "members": g.get("members", 0)} for g in live["guilds"][:8]]} if live else None
    return web.json_response({
        "bot": b, "ready": ready, "checks": checks, "info": info, "live": summary,
        "can_start": ready and b["state"] in ("stopped", "crashed"),
        "invite": invite_url(vals["DISCORD_TOKEN"]) if vals["DISCORD_TOKEN"] else None,
    })


async def bot_start(request: web.Request):
    bot, root = request.app["bot"], request.app["root"]
    checks, _ = readiness(root)
    if not all(c["ok"] for c in checks):
        return _err("Relay isn't ready to start yet", 409, checks=checks)
    if bot.running:
        return _err("already running", 409)
    await bot.start()
    return web.json_response({"ok": True})


async def bot_stop(request: web.Request):
    await request.app["bot"].stop()
    return web.json_response({"ok": True})


async def bot_restart(request: web.Request):
    bot, root = request.app["bot"], request.app["root"]
    checks, _ = readiness(root)
    if not all(c["ok"] for c in checks):
        return _err("Relay isn't ready to start yet", 409, checks=checks)
    await bot.stop()
    await bot.start()
    return web.json_response({"ok": True})


async def logs(request: web.Request):
    src = request.query.get("source", "bot")
    name = LOG_FILES.get(src)
    if name is None:
        return _err("unknown log source")
    path = request.app["root"] / "logs" / name
    if not path.exists():
        return web.json_response({"ok": True, "text": "", "offset": 0, "reset": False, "exists": False})
    size = path.stat().st_size
    try:
        offset = int(request.query["offset"]) if "offset" in request.query else None
    except ValueError:
        return _err("bad offset")
    reset = False
    if offset is None or offset > size:
        reset = offset is not None
        offset = max(0, size - 64 * 1024)
    end = min(size, offset + MAX_CHUNK)
    with open(path, "rb") as f:
        f.seek(offset)
        raw = f.read(end - offset)
    text = raw.decode("utf-8", "replace")
    if offset > 0 and "offset" not in request.query:
        text = text.split("\n", 1)[-1]
    return web.json_response({"ok": True, "text": scrub(text), "offset": end, "reset": reset, "exists": True, "size": size})


async def get_config(request: web.Request):
    root = request.app["root"]
    try:
        raw = load_raw_config(root)
    except (OSError, json.JSONDecodeError) as e:
        return _err(f"couldn't read config.json: {e}", 500)
    return web.json_response({"ok": True, "config": raw, "schema": Config.model_json_schema(),
                              "running": request.app["bot"].running})


async def _validated(data) -> tuple[dict | None, web.Response | None]:
    if not isinstance(data, dict):
        return None, _err("config must be a JSON object")
    try:
        Config.model_validate(data)
    except ValidationError as e:
        return None, _err("invalid config", 422, errors=_fmt_errors(e))
    return data, None


async def validate_config(request: web.Request):
    body = await request.json()
    _, bad = await _validated(body.get("config"))
    return bad or web.json_response({"ok": True})


async def put_config(request: web.Request):
    body = await request.json()
    data, bad = await _validated(body.get("config"))
    if bad:
        return bad
    save_config(request.app["root"], data)
    return web.json_response({"ok": True, "restart_required": request.app["bot"].running})


async def patch_config(request: web.Request):
    root = request.app["root"]
    body = await request.json()
    patch = body.get("patch")
    if not isinstance(patch, dict):
        return _err("patch must be an object")
    merged = deep_merge(load_raw_config(root), patch)
    data, bad = await _validated(merged)
    if bad:
        return bad
    save_config(root, data)
    return web.json_response({"ok": True, "restart_required": request.app["bot"].running})


async def get_env(request: web.Request):
    root = request.app["root"]
    vals = env_values(root)
    checks, cfg = readiness(root)
    out = []
    for v in ENV_VARS:
        cur = vals[v["key"]]
        needed = v["need"] == "always" or (v["need"] == "turso" and cfg is not None and cfg.storage.mode == "turso")
        out.append({"key": v["key"], "label": v["label"], "help": v["help"], "secret": v["secret"], "set": bool(cur),
                    "needed": needed, "generate": v.get("generate", False),
                    "display": (mask(cur) if v["secret"] else cur) if cur else ""})
    return web.json_response({"ok": True, "vars": out, "running": request.app["bot"].running})


async def put_env(request: web.Request):
    body = await request.json()
    values = body.get("values")
    if not isinstance(values, dict):
        return _err("values must be an object")
    updates: dict[str, str | None] = {}
    for k, v in values.items():
        if k not in ENV_KEYS:
            return _err(f"{k} isn't an editable variable")
        if v is None:
            continue
        if not isinstance(v, str):
            return _err(f"{k} must be text")
        v = v.strip()
        updates[k] = v if v else None
    try:
        write_env(request.app["root"] / ".env", updates)
    except ValueError as e:
        return _err(str(e))
    return web.json_response({"ok": True, "restart_required": request.app["bot"].running})


async def personality_preview(request: web.Request):
    body = await request.json()
    try:
        p = PersonalityCfg.model_validate(body.get("personality") or {})
    except ValidationError as e:
        return _err("invalid", 422, errors=_fmt_errors(e))
    return web.json_response({"ok": True, "text": personality_block(p, str(body.get("name") or "Relay"))})


async def on_cleanup(app: web.Application):
    await app["bot"].stop()


def create_app(root: Path = ROOT, token: str | None = None, argv: list[str] | None = None, shell: str | None = None) -> web.Application:
    app = web.Application(middlewares=[guard], client_max_size=12 * 1024 * 1024)
    app["root"] = Path(root)
    app["token"] = token or secrets.token_urlsafe(24)
    app["shell"] = shell
    app["bot"] = BotProcess(Path(root), argv)
    r = app.router
    r.add_get("/", index)
    r.add_get("/api/status", status)
    r.add_post("/api/bot/start", bot_start)
    r.add_post("/api/bot/stop", bot_stop)
    r.add_post("/api/bot/restart", bot_restart)
    r.add_get("/api/logs", logs)
    r.add_get("/api/config", get_config)
    r.add_post("/api/config/validate", validate_config)
    r.add_put("/api/config", put_config)
    r.add_patch("/api/config", patch_config)
    r.add_get("/api/env", get_env)
    r.add_put("/api/env", put_env)
    r.add_post("/api/personality/preview", personality_preview)
    add_chat_routes(app)
    add_custom_routes(app)
    app.on_cleanup.append(on_cleanup)
    return app
