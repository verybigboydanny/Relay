from __future__ import annotations

import types
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from relay.commands.chat import Chat
from relay.tools.blueprints import TEMPLATES, STAFF_PERMS, SetupServer, _outline, _plan_cats
from relay.services import permission_service as ps
from relay.utils.errors import PlanError
from tests.dfake import add_role, mk_channel, mk_member
from tests.helpers import test_tools, world  # noqa: F401
from tests.test_pipeline import groq, plan, w  # noqa: F401


@pytest.fixture
async def s(app, w):
    g = w.g
    made = {"roles": [], "voice": [], "text": [], "cats": []}

    async def create_role(**kw):
        r = add_role(g, kw["name"], 3, kw["permissions"])
        r.colour, r.hoist = kw["colour"], kw["hoist"]
        made["roles"].append(r)
        return r

    async def create_voice(name, category=None, user_limit=0, reason=None):
        c = mk_channel(g, name, discord.VoiceChannel, category=category)
        made["voice"].append((name, user_limit))
        return c

    async def create_text(name, category=None, topic=None, overwrites=None, reason=None):
        c = mk_channel(g, name, category=category)
        c.topic, c.created_with = topic, overwrites
        made["text"].append(c)
        return c

    async def create_cat(name, overwrites=None, reason=None):
        c = mk_channel(g, name, discord.CategoryChannel)
        c.created_with = overwrites
        made["cats"].append(c)
        return c

    g.create_role = AsyncMock(side_effect=create_role)
    g.create_voice_channel = AsyncMock(side_effect=create_voice)
    g.create_text_channel = AsyncMock(side_effect=create_text)
    g.create_category = AsyncMock(side_effect=create_cat)
    w.made = made
    return w


async def test_open_ended_request_gets_a_rich_setup_with_one_confirmation(app, s):
    groq(app, plan({"tool": "setup_server", "args": {"template": "gaming"}}))
    r = await app.assistant.handle("Can you set this server up as a gaming server", s.g, s.admin, s.ch)
    assert r.confirmation and "Squad 1" in r.text and "Staff (private)" in r.text and "Ready to set this server up as a gaming server" in r.text
    assert len([c for c in app.groq.calls if c[0] == "json"]) == 1
    done = await app.assistant.confirm(r.confirmation["id"], s.admin, s.g, s.ch, True)
    m = s.made
    assert [x.name for x in m["roles"]] == ["Gamer", "Moderator"]
    assert [c.name for c in m["cats"]] == ["📌 Info", "💬 Lounge", "🎮 Games", "🛡️ Staff"]
    names = [c.name for c in m["text"]]
    assert "👋・welcome" in names and "🤝・looking-for-group" in names and len(names) + len(m["voice"]) >= 15
    assert ("🎮 Squad 1", 4) in m["voice"]
    assert "Your server is now set up as a gaming server." in done.text and "(read-only)" in done.text
    rules = next(c for c in m["text"] if c.name.endswith("rules"))
    pairs = {t.id: ps.ow_pair(o) for t, o in rules.created_with.items()}
    assert pairs[s.g.default_role.id][1] & ps.FLAGS["send_messages"] and pairs[s.g.me.id][0] & ps.FLAGS["send_messages"]
    staff_cat = m["cats"][-1]
    sp = {t.id: ps.ow_pair(o) for t, o in staff_cat.created_with.items()}
    assert sp[s.g.default_role.id][1] & ps.FLAGS["view_channel"] and sp[m["roles"][1].id][0] & ps.FLAGS["view_channel"]
    assert m["roles"][1].permissions.kick_members and not m["roles"][1].permissions.administrator and m["roles"][1].hoist
    rows = await app.audit.search(s.g.id, action="setup_server", limit=100)
    assert len(rows) > 15


async def test_setup_skips_what_already_exists_and_is_idempotent(app, s):
    counts = []
    for _ in range(2):
        groq(app, plan({"tool": "setup_server", "args": {"template": "community", "fancy_names": False, "staff_area": False}}))
        r = await app.assistant.handle("set up a community server", s.g, s.admin, s.ch)
        out = await app.assistant.confirm(r.confirmation["id"], s.admin, s.g, s.ch, True)
        counts.append(s.g.create_text_channel.await_count + s.g.create_voice_channel.await_count + s.g.create_category.await_count)
    assert "nothing to add" in out.text and counts[0] == counts[1] and counts[0] > 10
    assert [x.name for x in s.made["roles"]] == ["Member"]


async def test_setup_options_rename_roles_and_add_channels(app, s):
    a = SetupServer(template="study", member_role="Scholar", extra_text_channels=["Exam Prep"], extra_voice_channels=["Late Night"])
    text = _outline(a)
    assert "Scholar" in text and "exam-prep" in text and "Late Night" in text
    cats = _plan_cats(a)
    assert any(c.title == "Study" and any(x.name == "exam-prep" for x in c.channels) for c in cats)


async def test_setup_needs_real_permissions_and_authority(app, s):
    groq(app, plan({"tool": "setup_server", "args": {"template": "gaming"}}))
    r = await app.assistant.handle("set up gaming", s.g, s.mod, s.ch)
    assert "can't do that" in r.text and s.g.create_category.await_count == 0
    s.g.me.guild_permissions = discord.Permissions.none()
    groq(app, plan({"tool": "setup_server", "args": {"template": "gaming"}}))
    r = await app.assistant.handle("set up gaming", s.g, s.admin, s.ch)
    assert "don't have the manage channels" in r.text


async def test_setup_survives_partial_failures_and_reports_them(app, s):
    real = s.g.create_text_channel.side_effect

    async def flaky(name, **kw):
        if "memes" in name:
            raise discord.HTTPException(MagicMock(status=400), "boom")
        return await real(name, **kw)
    s.g.create_text_channel.side_effect = flaky
    groq(app, plan({"tool": "setup_server", "args": {"template": "gaming"}}))
    r = await app.assistant.handle("set up gaming", s.g, s.admin, s.ch)
    out = await app.assistant.confirm(r.confirmation["id"], s.admin, s.g, s.ch, True)
    assert "most of a gaming server" in out.text and "Couldn't create" in out.text and "welcome" in out.text
    rows = await app.audit.search(s.g.id, action="setup_server", limit=100)
    assert any(x["status"] == "partial" for x in rows)


def test_every_template_is_sane():
    for name, t in TEMPLATES.items():
        cats = t["categories"]
        assert cats[0].title == "Info" and cats[-1].staff
        total = sum(len(c.channels) for c in cats)
        assert 10 <= total <= 25, name
        for c in cats:
            names = [ch.name for ch in c.channels]
            assert len(names) == len(set(names))
            assert all(len(f"{ch.emoji}・{ch.name}") <= 100 for ch in c.channels)
    assert set(STAFF_PERMS) <= set(ps.FLAGS) and "administrator" not in STAFF_PERMS


def make_message(bot, content, mentions=(), reply_to=None):
    m = MagicMock(spec=discord.Message)
    m.author = MagicMock(spec=discord.Member)
    m.author.bot = False
    m.guild = MagicMock()
    m.content = content
    m.mentions = list(mentions)
    m.reference = None
    m.reply = AsyncMock()
    m.channel = MagicMock()
    m.channel.typing = MagicMock(return_value=types.SimpleNamespace(__aenter__=AsyncMock(), __aexit__=AsyncMock()))
    if reply_to is not None:
        m.reference = types.SimpleNamespace(message_id=5, resolved=reply_to)
    return m


@pytest.fixture
def chat(app):
    bot = MagicMock()
    bot.user = types.SimpleNamespace(id=42)
    bot.app = app
    bot.settings = app.settings
    seen = []

    async def handle(text, guild, member, channel):
        seen.append(text)
        from relay.assistant import Reply
        return Reply("ok")
    app.assistant.handle = handle
    c = Chat(bot)
    c.seen = seen
    return c, bot


class Typing:
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False


async def fire(chat_cog, msg):
    msg.channel.typing = lambda: Typing()
    await chat_cog.on_message(msg)


async def test_mention_anywhere_and_replies_to_the_bot_are_handled(chat):
    c, bot = chat
    await fire(c, make_message(bot, "<@42> create a channel", mentions=[bot.user]))
    await fire(c, make_message(bot, "hey <@42> what's up", mentions=[bot.user]))
    prior = MagicMock(spec=discord.Message)
    prior.author = types.SimpleNamespace(id=42)
    await fire(c, make_message(bot, "Any of your choice", reply_to=prior))
    assert c.seen == ["create a channel", "hey  what's up", "Any of your choice"]


async def test_other_people_and_other_bots_are_ignored(chat):
    c, bot = chat
    await fire(c, make_message(bot, "just chatting"))
    stranger = MagicMock(spec=discord.Message)
    stranger.author = types.SimpleNamespace(id=999)
    await fire(c, make_message(bot, "reply to someone else", reply_to=stranger))
    await fire(c, make_message(bot, "r!status <@42>", mentions=[bot.user]))
    other = make_message(bot, "<@42> hi", mentions=[bot.user])
    other.author.bot = True
    await fire(c, other)
    assert c.seen == []


async def test_reply_to_deleted_message_is_fetched_or_ignored(chat):
    c, bot = chat
    prior = MagicMock(spec=discord.Message)
    prior.author = types.SimpleNamespace(id=42)
    m = make_message(bot, "answer", reply_to=types.SimpleNamespace())
    m.reference = types.SimpleNamespace(message_id=5, resolved=None)
    m.channel.fetch_message = AsyncMock(return_value=prior)
    await fire(c, m)
    assert c.seen == ["answer"]
    m2 = make_message(bot, "answer2")
    m2.reference = types.SimpleNamespace(message_id=5, resolved=None)
    m2.channel.fetch_message = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "gone"))
    await fire(c, m2)
    assert c.seen == ["answer"]


async def test_multi_step_results_are_a_clean_bullet_list(app, w):
    groq(app, plan({"tool": "create_category", "args": {"name": "Gaming"}}, {"tool": "create_channel", "args": {"name": "chat", "category": "Gaming"}}))

    async def cat(name, overwrites=None, reason=None):
        return mk_channel(w.g, name, discord.CategoryChannel)

    async def text(name, category=None, topic=None, overwrites=None, reason=None):
        return mk_channel(w.g, name, category=category)
    w.g.create_category = AsyncMock(side_effect=cat)
    w.g.create_text_channel = AsyncMock(side_effect=text)
    r = await app.assistant.handle("make a gaming category with a chat channel", w.g, w.admin, w.ch)
    assert r.text.startswith("All set. Here's what I did:\n• Created category Gaming.\n• Created #chat under Gaming.")
