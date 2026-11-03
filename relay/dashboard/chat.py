"""HTTP handlers for the in-app chat with Relay."""
from __future__ import annotations

import logging

from aiohttp import web

from relay.dashboard import bridge_client as bc
from relay.dashboard import chat_core as cc
from relay.logging_setup import scrub

log = logging.getLogger(__name__)

GREETING = ("I can start, stop and restart myself, read my logs, change my settings, and make changes in your Discord servers. "
            "Tell me what you need.")
GREETING_SUGGEST = ["Make a channel called test", "Show me my recent logs", "What can you do in Discord?"]


def _store(app: web.Application) -> cc.ChatStore:
    return cc.ChatStore(app["root"] / "data" / "dashboard_chat.json")


def _fail(msg: str, status: int = 400, **extra) -> web.Response:
    return web.json_response({"ok": False, "error": msg, **extra}, status=status)


def _log_tail(root, n: int = 30) -> list[str]:
    for name in ("relay.log", "console.log"):
        try:
            data = (root / "logs" / name).read_bytes()[-14000:].decode("utf-8", "replace")
        except OSError:
            continue
        lines = [scrub(x) for x in data.splitlines() if x.strip()]
        if lines:
            return lines[-n:]
    return []


def _tools() -> list[str]:
    try:
        import relay.tools  # registers the tools
        from relay.tools.base import REGISTRY
        return [f"{t.name} ({cc.clip(t.desc, 60)})" for t in REGISTRY.tools.values()]
    except Exception:
        return []


async def _ai(app: web.Application, cfg, key: str):
    sig = (key, cfg.groq.model, cfg.groq.temperature, cfg.groq.max_tokens, cfg.groq.reasoning_effort,
           cfg.groq.json_mode, cfg.groq.timeout_seconds)
    cur = app.get("chat_ai")
    if cur and cur[0] == sig:
        return cur[1]
    from relay.ai.groq_client import GroqClient
    if cur:
        try:
            await cur[1].client.close()
        except Exception:
            pass
    client = GroqClient(key, cfg.groq)
    app["chat_ai"] = (sig, client)
    return client


def _greeted(app: web.Application) -> list[dict]:
    store = _store(app)
    msgs = store.load()
    if not msgs:
        msgs = store.append(cc.make_msg("relay", GREETING, kind="event", suggest=GREETING_SUGGEST))
    return msgs


def _servers(root) -> list[dict]:
    st = bc.read_state(root)
    return [{"id": g["id"], "name": g["name"], "members": g.get("members", 0)} for g in (st or {}).get("guilds", [])]


def _digits(v) -> str | None:
    v = str(v or "").strip()
    return v if v.isdigit() and len(v) < 25 else None


async def get_chat(request: web.Request):
    from relay.dashboard import server as S
    app = request.app
    key = S.env_values(app["root"])["GROQ_API_KEY"]
    return web.json_response({"ok": True, "messages": _greeted(app), "ai_ready": bool(key.strip()),
                              "servers": _servers(app["root"]), "online": app["bot"].status()["state"] == "online"})


def _result_msg(res: dict, guild_id: str | None):
    """A chat message from the bot's answer to a Discord request."""
    if not res.get("ok"):
        return cc.make_msg("relay", res.get("error") or "That didn't work.", kind="discord", failed=True)
    text = str(res.get("text") or "Done.")
    if res.get("lines"):
        text += "\n" + "\n".join(f"- {x}" for x in res["lines"][:40])
    gid = res.get("guild_id") or guild_id
    extra = {"via": res.get("guild")}
    if res.get("confirmation"):
        extra["confirm"] = {"cid": res["confirmation"]["cid"], "guild_id": gid, "channel_id": res.get("channel_id"), "state": "pending"}
    if res.get("ambiguity"):
        extra["pick"] = {"token": res["ambiguity"]["token"], "options": res["ambiguity"]["options"], "guild_id": gid,
                         "channel_id": res.get("channel_id"), "state": "pending"}
    return cc.make_msg("relay", text, kind="discord", **extra)


async def _do_discord(app, request_text: str, guild_id: str | None) -> dict:
    """Send a request to the live bot. Returns the message to show."""
    state = app["bot"].status()["state"]
    if state != "online" or bc.read_state(app["root"]) is None:
        if state in ("stopped", "crashed"):
            act = [{"id": cc._id(), "type": "bot", "op": "start", "label": "Start Relay", "state": "pending"}]
            return cc.make_msg("relay", "I'm offline right now, so I can't reach Discord. Start me and ask again.", kind="discord", actions=act, failed=True)
        return cc.make_msg("relay", "I'm still connecting to Discord. Give me a few seconds and ask again.", kind="discord", failed=True)
    res = await bc.call(app["root"], {"type": "act", "text": request_text, "guild_id": guild_id})
    return _result_msg(res, guild_id)


async def post_chat(request: web.Request):
    from relay.dashboard import server as S
    from relay.config import Config
    from relay.utils.errors import AIError
    app, root = request.app, request.app["root"]
    body = await request.json()
    text = str(body.get("message") or "").strip()
    guild_id = _digits(body.get("guild_id"))
    if not text:
        return _fail("empty message")
    if len(text) > cc.MAX_IN:
        return _fail(f"That's too long. Keep it under {cc.MAX_IN} characters.")
    store = _store(app)
    _greeted(app)
    msgs = store.append(cc.make_msg("user", text))
    vals = S.env_values(root)
    key = vals["GROQ_API_KEY"].strip()
    checks, cfg = S.readiness(root)
    if not key or cfg is None:
        why = "I can't think without my Groq API key. Add it in the Environment tab and I'll be right here." if not key \
            else "My config has a problem, so I can't start thinking yet. Open the Config tab and I'll be able to talk."
        msgs = store.append(cc.make_msg("relay", why, kind="event"))
        return web.json_response({"ok": True, "messages": msgs})
    info = {"name": cfg.bot.name, "model": cfg.groq.model, "storage": cfg.storage.mode, "prefix": cfg.bot.prefix, "timezone": cfg.timezone,
            "confirmations": cfg.confirmation.direct, "actions_per_plan": cfg.limits.actions_per_plan}
    ctx = cc.context_text(status=app["bot"].status(), info=info, checks=checks, log_tail=_log_tail(root), tools=_tools(),
                          personality=cfg.personality.model_dump(exclude_defaults=True),
                          servers=_servers(root), selected=guild_id)
    base = S.load_raw_config(root)

    def valid(patch: dict) -> bool:
        Config.model_validate(S.deep_merge(base, patch))
        return True

    try:
        from relay.ai.groq_client import extract_json
        client = await _ai(app, cfg, key)
        raw = await client.chat_json(cc.to_model_messages(cc.chat_system(cfg.bot.name, ctx), msgs))
        try:
            data = extract_json(raw)
        except ValueError:
            data = {"reply": raw}
    except AIError as e:
        return web.json_response({"ok": False, "error": e.user_message, "messages": store.load()})
    except Exception:
        log.exception("chat failed")
        return web.json_response({"ok": False, "error": "Something went wrong while I was thinking. Try again.", "messages": store.load()})
    shaped = cc.shape_reply(data, valid, app["bot"].status()["state"])
    reply = shaped["text"] or ("On it." if shaped["discord"] else "Hmm, I lost my train of thought. Say that again?")
    new = [cc.make_msg("relay", reply, actions=shaped["actions"], suggest=[] if shaped["discord"] else shaped["suggest"])]
    if shaped["show_logs"]:
        tail = _log_tail(root, shaped["show_logs"])
        body_txt = "```\n" + "\n".join(cc.clip(x, 300) for x in tail) + "\n```" if tail else "My log is empty so far."
        new.append(cc.make_msg("relay", f"Last {len(tail)} lines of my log:\n{body_txt}" if tail else body_txt, kind="chat"))
    msgs = store.append(*new)
    if shaped["discord"]:
        res_msg = await _do_discord(app, shaped["discord"], guild_id)
        res_msg["suggest"] = shaped["suggest"]
        msgs = store.append(res_msg)
    return web.json_response({"ok": True, "messages": msgs})


async def post_discord(request: web.Request):
    """Confirm/cancel a pending Discord action, or answer a 'which one?' question."""
    app, root = request.app, request.app["root"]
    body = await request.json()
    store = _store(app)
    msgs = store.load()
    m = store.find(msgs, str(body.get("msg_id")))
    if m is None:
        return _fail("I don't have that any more.", 404)
    kind = body.get("kind")
    if kind == "confirm":
        blk = m.get("confirm")
        if not blk or blk.get("state") != "pending":
            return _fail("That one's already been handled.", 409)
        approve = bool(body.get("approve"))
        blk["state"] = "approved" if approve else "cancelled"
        store.save(msgs)
        res = await bc.call(root, {"type": "confirm", "cid": blk["cid"], "approve": approve, "guild_id": blk["guild_id"], "channel_id": blk["channel_id"]})
    elif kind == "pick":
        blk = m.get("pick")
        choice = str(body.get("choice"))
        if not blk or blk.get("state") != "pending" or choice not in {o["id"] for o in blk["options"]}:
            return _fail("That's not one of the options.", 409)
        blk["state"], blk["chosen"] = "chosen", choice
        store.save(msgs)
        res = await bc.call(root, {"type": "pick", "token": blk["token"], "choice": choice, "guild_id": blk["guild_id"], "channel_id": blk["channel_id"]})
    else:
        return _fail("unknown request")
    return web.json_response({"ok": True, "messages": store.append(_result_msg(res, m.get("confirm", m.get("pick", {})).get("guild_id")))})


async def post_action(request: web.Request):
    from relay.dashboard import server as S
    from relay.config import Config
    from pydantic import ValidationError
    app, root = request.app, request.app["root"]
    body = await request.json()
    store = _store(app)
    msgs = store.load()
    msg, act = store.find_action(msgs, str(body.get("msg_id")), str(body.get("action_id")))
    if act is None:
        return _fail("I don't have that action any more.", 404)
    if act.get("state") != "pending":
        return _fail("That one's already been used.", 409)
    bot = app["bot"]
    note, follow = "", []
    if act["type"] == "bot":
        op = act["op"]
        if op in ("start", "restart"):
            checks, _ = S.readiness(root)
            if not all(c["ok"] for c in checks):
                return _fail("I'm not ready to start yet. Check the Home tab.", 409)
        if op == "start" and bot.running:
            return _fail("I'm already running.", 409)
        if op == "stop":
            await bot.stop()
            note = "Okay, I've stopped."
        elif op == "restart":
            await bot.stop()
            await bot.start()
            note = "Restarting now. Give me a few seconds."
        else:
            await bot.start()
            note = "Starting up. I'll tell you when I'm connected."
    elif act["type"] == "config":
        try:
            merged = S.deep_merge(S.load_raw_config(root), act["patch"])
            Config.model_validate(merged)
        except (ValidationError, OSError, ValueError) as e:
            return _fail(f"That change isn't valid any more: {str(e)[:120]}", 422)
        S.save_config(root, merged)
        if bot.running:
            note = "Saved. It takes effect the next time I start."
            follow = [{"id": cc._id(), "type": "bot", "op": "restart", "label": "Restart now", "state": "pending"}]
        else:
            note = "Saved. It'll be in effect when I start."
    act["state"] = "done"
    store.save(msgs)
    msgs = store.append(cc.make_msg("relay", note, kind="event", actions=follow))
    return web.json_response({"ok": True, "messages": msgs})


async def post_notify(request: web.Request):
    app = request.app
    body = await request.json()
    text = cc.clip(body.get("text") or "", 400)
    if not text:
        return _fail("empty")
    suggest = [cc.clip(x, 60) for x in (body.get("suggest") or []) if isinstance(x, str)][:3]
    store = _store(app)
    msgs = _greeted(app)
    last = msgs[-1] if msgs else None
    if last and last["role"] == "relay" and last["text"] == text and cc.now() - last["ts"] < 15:
        return web.json_response({"ok": True, "messages": msgs})
    return web.json_response({"ok": True, "messages": store.append(cc.make_msg("relay", text, kind="event", suggest=suggest))})


async def post_clear(request: web.Request):
    _store(request.app).save([])
    return web.json_response({"ok": True, "messages": _greeted(request.app)})


async def on_cleanup(app: web.Application):
    cur = app.get("chat_ai")
    if cur:
        try:
            await cur[1].client.close()
        except Exception:
            pass


def add_routes(app: web.Application) -> None:
    r = app.router
    r.add_get("/api/chat", get_chat)
    r.add_post("/api/chat", post_chat)
    r.add_post("/api/chat/action", post_action)
    r.add_post("/api/chat/discord", post_discord)
    r.add_post("/api/chat/notify", post_notify)
    r.add_post("/api/chat/clear", post_clear)
    app.on_cleanup.append(on_cleanup)
