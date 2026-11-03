from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Literal

import discord
from pydantic import BaseModel, ConfigDict, Field

from relay.utils.errors import PlanError

if TYPE_CHECKING:
    from relay.app import App

Risk = Literal["read", "low", "medium", "high"]


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid", coerce_numbers_to_str=True)


def ref(kind: str, default: Any = ..., desc: str = ""):
    return Field(default, description=desc, json_schema_extra={"ref": kind})


def ref_fields(model: type[BaseModel]) -> dict[str, str]:
    out = {}
    for name, f in model.model_fields.items():
        extra = f.json_schema_extra
        if isinstance(extra, dict) and "ref" in extra:
            out[name] = extra["ref"]
    return out


@dataclass
class Item:
    target_id: int | None
    ok: bool
    text: str
    error_code: str | None = None


@dataclass
class Result:
    text: str
    ok: bool = True
    resource_id: int | None = None
    resource_type: str | None = None
    items: list[Item] = field(default_factory=list)
    data: dict = field(default_factory=dict)
    entities: dict = field(default_factory=dict)
    touched: list[int] = field(default_factory=list)
    summarize: bool = False
    undo_id: str | None = None
    details: dict = field(default_factory=dict)
    event: tuple[str, str] | None = None

    @property
    def status(self) -> str:
        if self.items:
            good = sum(i.ok for i in self.items)
            if good == len(self.items):
                return "success"
            return "partial" if good else "failure"
        return "success" if self.ok else "failure"


@dataclass
class Refs:
    values: dict[str, Any] = field(default_factory=dict)
    labels: dict[str, Any] = field(default_factory=dict)

    def one(self, name: str) -> int | None:
        v = self.values.get(name)
        if isinstance(v, list):
            return v[0] if v else None
        return v

    def many(self, name: str) -> list[int]:
        v = self.values.get(name)
        if v is None:
            return []
        return list(v) if isinstance(v, list) else [v]

    def label(self, name: str) -> str:
        v = self.labels.get(name)
        if isinstance(v, list):
            return ", ".join(v)
        return v or ""


@dataclass
class Tool:
    name: str
    desc: str
    args: type[Args]
    run: Callable[..., Awaitable[Result]]
    risk: Risk = "low"
    access: Literal["open", "staff", "admin"] = "staff"
    perm: str | None = None
    confirm: Literal["policy", "always"] = "policy"
    tags: frozenset[str] = frozenset()
    describe: Callable[[Args, Refs], str] | None = None
    guard: Callable[..., Awaitable[None]] | None = None
    confirm_if: Callable[[Args, Refs], bool] | None = None
    verify: Callable[..., Awaitable[bool | None]] | None = None
    idempotent: bool = False
    schedulable: bool = True
    creates: str | None = None
    timeout: float | None = None

    def preview(self, a: Args, r: Refs) -> str:
        if self.describe:
            return self.describe(a, r)
        parts = []
        for k, v in a.model_dump(exclude_none=True).items():
            parts.append(f"{k}={r.label(k) or v}")
        return f"{self.name} " + ", ".join(parts)

    def signature(self) -> str:
        schema = self.args.model_json_schema()
        req = set(schema.get("required", []))
        parts = []
        for k, p in schema.get("properties", {}).items():
            parts.append(f"{k}{'' if k in req else '?'}: {_ptype(p, schema)}")
        return f"{self.name}({', '.join(parts)}) - {self.desc}"


def _ptype(p: dict, schema: dict) -> str:
    if "ref" in p:
        many = p.get("type") == "array" or any(o.get("type") == "array" for o in p.get("anyOf", []))
        return f"[{p['ref']}-ref]" if many else f"{p['ref']}-ref"
    if "$ref" in p:
        return "obj"
    if "enum" in p:
        return "|".join(str(x) for x in p["enum"])
    if "anyOf" in p:
        opts = [o for o in p["anyOf"] if o.get("type") != "null"]
        return _ptype(opts[0], schema) if opts else "any"
    t = p.get("type", "any")
    if t == "array":
        inner = p.get("items", {})
        base = f"{p['ref']}-ref" if "ref" in p else _ptype(inner, schema)
        return f"[{base}]"
    if "ref" in p:
        return f"{p['ref']}-ref"
    return {"string": "str", "integer": "int", "boolean": "bool", "number": "num", "object": "obj"}.get(t, t)


class Registry:
    def __init__(self):
        self.tools: dict[str, Tool] = {}

    def add(self, t: Tool) -> Tool:
        if t.name in self.tools:
            raise ValueError(f"duplicate tool {t.name}")
        self.tools[t.name] = t
        return t

    def get(self, name: str) -> Tool:
        t = self.tools.get(name)
        if t is None:
            raise PlanError(f"unknown tool '{name}'", user_message=f"I tried to use a tool that doesn't exist ({name[:40]}).")
        return t

    def catalog(self) -> str:
        return "\n".join(f"- {t.signature()}" for t in self.tools.values())


REGISTRY = Registry()


def tool(name: str, desc: str, args: type[Args], **kw):
    def deco(fn):
        REGISTRY.add(Tool(name=name, desc=desc, args=args, run=fn, tags=frozenset(kw.pop("tags", ())), **kw))
        return fn
    return deco


@dataclass
class Ctx:
    app: "App"
    guild: discord.Guild
    actor: discord.Member | None
    actor_id: int
    channel: Any
    source: str
    request_id: str
    plan_id: str
    settings: Any
    tz_name: str
    actor_type: str = "user"
    task_id: str | None = None
    results: dict[str, Result] = field(default_factory=dict)
    allowed_ids: set[int] = field(default_factory=set)
    entities: dict = field(default_factory=dict)
    confirmed: bool = False

    @property
    def channel_id(self) -> int | None:
        return getattr(self.channel, "id", None)

    @property
    def me(self) -> discord.Member:
        return self.guild.me

    def role(self, rid: int) -> discord.Role | None:
        return self.guild.get_role(rid)

    def gchannel(self, cid: int):
        return self.guild.get_channel(cid)

    async def member(self, uid: int) -> discord.Member | None:
        m = self.guild.get_member(uid)
        if m:
            return m
        try:
            return await self.guild.fetch_member(uid)
        except discord.NotFound:
            return None

    def reason(self, text: str | None) -> str:
        who = f"{self.actor} ({self.actor_id})" if self.actor else str(self.actor_id)
        base = (text or "no reason given").strip()
        return f"{base} | via {self.app.cfg.bot.name} for {who}"[:512]

    def is_admin(self) -> bool:
        return bool(self.actor) and self.app.auth.is_admin(self.actor, self.settings)


_marks: dict[int, float] = {}


def mark(ids, ttl: float = 30.0) -> None:
    t = time.monotonic() + ttl
    for i in ids:
        if i:
            _marks[int(i)] = t
    if len(_marks) > 2000:
        for k in [k for k, v in _marks.items() if v < time.monotonic()]:
            _marks.pop(k, None)


def recently_touched(i: int) -> bool:
    return _marks.get(int(i), 0) > time.monotonic()
