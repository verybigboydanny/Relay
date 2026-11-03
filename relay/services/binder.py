from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import timedelta

from pydantic import ValidationError

from relay.ai.schemas import PlannerOut, StepIn, When
from relay.config import Config
from relay.services.member_resolver import Env, Resolver
from relay.tools.base import Refs, Registry, ref_fields
from relay.utils import time_parser as tp
from relay.utils.errors import Ambiguous, PlanError, Unresolved

CREATE_FIELD = {"category": "category", "channel": "channel", "channel_or_category": "channel", "role": "role"}


@dataclass
class BoundStep:
    id: str
    tool: str
    args: dict
    refs: dict
    labels: dict
    depends_on: list[str]
    when: dict | None
    preview: str

    def to_dict(self) -> dict:
        return {"id": self.id, "tool": self.tool, "args": self.args, "refs": self.refs, "labels": self.labels,
                "depends_on": self.depends_on, "when": self.when, "preview": self.preview}

    @classmethod
    def from_dict(cls, d: dict) -> "BoundStep":
        return cls(d["id"], d["tool"], d["args"], d["refs"], d["labels"], d["depends_on"], d.get("when"), d["preview"])


@dataclass
class BoundPlan:
    steps: list[BoundStep]
    plan_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    note: str | None = None

    def to_dict(self) -> dict:
        return {"plan_id": self.plan_id, "note": self.note, "steps": [s.to_dict() for s in self.steps]}

    @classmethod
    def from_dict(cls, d: dict) -> "BoundPlan":
        return cls([BoundStep.from_dict(s) for s in d["steps"]], d["plan_id"], d.get("note"))


def order_steps(steps: list[BoundStep]) -> list[BoundStep]:
    by_id = {s.id: s for s in steps}
    for s in steps:
        for d in s.depends_on:
            if d not in by_id:
                raise PlanError(f"step {s.id} depends on unknown step {d}")
    done, out, visiting = set(), [], set()

    def visit(s: BoundStep):
        if s.id in done:
            return
        if s.id in visiting:
            raise PlanError("circular dependency in plan")
        visiting.add(s.id)
        for d in s.depends_on:
            visit(by_id[d])
        visiting.discard(s.id)
        done.add(s.id)
        out.append(s)

    for s in steps:
        visit(s)
    return out


class Binder:
    def __init__(self, cfg: Config, registry: Registry, resolver: Resolver | None = None):
        self.cfg = cfg
        self.reg = registry
        self.resolver = resolver or Resolver()

    def parse_when(self, when: When, tz_name: str, tool_name: str, now=None) -> dict:
        tz = tp.tz_of(tz_name)
        now = now or tp.now()
        if when.in_ is not None:
            at = now + tp.parse_duration(when.in_)
            return {"kind": "once", "run_at": tp.iso(at), "delay_s": (at - now).total_seconds()}
        if when.at is not None:
            at = tp.parse_local(when.at, tz)
            if at <= now + timedelta(seconds=5):
                raise PlanError("time in the past", user_message=f"{tp.fmt_local(at, tz)} is already in the past. What time did you mean?")
            return {"kind": "once", "run_at": tp.iso(at)}
        rule = when.every.model_dump(exclude_none=True)
        if rule["freq"] == "interval":
            if tp.parse_duration(rule["interval"]).total_seconds() < self.cfg.scheduler.min_recurring_seconds:
                raise PlanError("interval too short",
                                user_message=f"Recurring tasks can't repeat more often than every {int(self.cfg.scheduler.min_recurring_seconds // 60)} minutes.")
        first = tp.next_occurrence(rule, now, tz)
        return {"kind": "every", "rule": rule, "run_at": tp.iso(first), "tz": tz_name}

    async def bind(self, out: PlannerOut, env: Env, tz_name: str) -> BoundPlan:
        if len(out.steps) > self.cfg.limits.actions_per_plan:
            raise PlanError("too many steps", user_message=f"That's more than {self.cfg.limits.actions_per_plan} actions in one go. Split it up.")
        planned: dict[tuple[str, str], str] = {}
        steps: list[BoundStep] = []
        for st in out.steps:
            steps.append(await self._bind_step(st, env, tz_name, planned))
        order_steps(steps)
        return BoundPlan(steps, note=out.note)

    async def _bind_step(self, st: StepIn, env: Env, tz_name: str, planned: dict) -> BoundStep:
        tool = self.reg.get(st.tool)
        try:
            args = tool.args.model_validate(st.args)
        except ValidationError as e:
            msg = "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()[:4])
            raise PlanError(f"invalid arguments for {st.tool}: {msg}", user_message=f"I couldn't build a valid request for {st.tool.replace('_', ' ')}.")
        refs: dict = {}
        labels: dict = {}
        deps = list(st.depends_on)
        for fname, kind in ref_fields(tool.args).items():
            val = getattr(args, fname)
            if val is None:
                continue
            many = isinstance(val, list)
            if many and len(val) > self.cfg.limits.max_bulk_targets:
                raise PlanError("bulk too large", user_message=f"That's more than {self.cfg.limits.max_bulk_targets} targets at once.")
            if many and not val:
                raise PlanError(f"{fname} is empty")
            ids, labs = [], []
            for v in (val if many else [val]):
                try:
                    r = await self.resolver.resolve(env, kind, v)
                    ids.append(r.id)
                    labs.append(r.label)
                except Unresolved:
                    fam = CREATE_FIELD.get(kind)
                    key = (fam, _norm(fam, v)) if fam else None
                    if key and key in planned:
                        ids.append(f"${planned[key]}")
                        labs.append(str(v))
                        if planned[key] not in deps:
                            deps.append(planned[key])
                    else:
                        raise
            refs[fname] = ids if many else ids[0]
            labels[fname] = labs if many else labs[0]
        if tool.creates and getattr(args, "name", None):
            fam = tool.creates
            planned[(fam, _norm(fam, args.name))] = st.id
            if fam == "channel":
                planned[("channel", _norm("channel", args.name))] = st.id
        when = None
        if st.when is not None:
            if not tool.schedulable:
                raise PlanError("not schedulable", user_message=f"{st.tool.replace('_', ' ')} can't be scheduled.")
            if tool.risk == "read":
                raise PlanError("read tool scheduled", user_message="Only actions can be scheduled, not lookups.")
            when = self.parse_when(st.when, tz_name, st.tool)
        r = Refs(refs, labels)
        return BoundStep(st.id, st.tool, args.model_dump(exclude_none=True), refs, labels, deps, when, tool.preview(args, r))


def _norm(fam: str | None, name: str) -> str:
    from relay.services.member_resolver import norm_channel, norm_name
    return norm_channel(name) if fam in ("channel",) else norm_name(name)
