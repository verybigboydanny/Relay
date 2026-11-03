from __future__ import annotations

import hashlib
import uuid
from datetime import timedelta
from typing import TYPE_CHECKING

from relay.services.binder import BoundPlan
from relay.storage.base import canonical, new_rec
from relay.utils.errors import Denied, PlanError, RelayError
from relay.utils.time_parser import iso, now

if TYPE_CHECKING:
    from relay.app import App


def plan_hash(plan: BoundPlan) -> str:
    return hashlib.sha256(canonical(plan.to_dict()).encode()).hexdigest()


class ConfirmationService:
    def __init__(self, app: "App"):
        self.app = app

    def step_needs(self, step, source: str, guild=None) -> bool:
        tool = self.app.registry.get(step.tool)
        if tool.risk == "read" or source == "scheduler":
            return False
        a = tool.args.model_validate(step.args)
        from relay.tools.base import Refs
        r = Refs(step.refs, step.labels)
        if tool.confirm == "always" or (tool.confirm_if and tool.confirm_if(a, r, guild)):
            return True
        c = self.app.cfg.confirmation
        mode = "recurring" if step.when and step.when["kind"] == "every" else "scheduled" if step.when else "direct"
        policy = getattr(c, mode)
        if mode == "direct" and source == "command" and not c.slash_requires_confirmation:
            policy = "never"
        if policy == "never":
            return False
        if policy == "always":
            return True
        return tool.risk in ("medium", "high")

    def required(self, plan: BoundPlan, source: str, guild=None) -> bool:
        return any(self.step_needs(s, source, guild) for s in plan.steps)

    async def create(self, plan: BoundPlan, *, guild_id: int, requester_id: int, channel_id: int, request_id: str,
                     source: str, request_text: str) -> dict:
        c = self.app.cfg.confirmation
        pending = await self.app.store.count("confirmations", guild_id=guild_id,
                                             where={"actor_id": requester_id, "status": "pending"})
        if pending >= c.max_pending_per_user:
            raise PlanError("too many pending", user_message=f"You already have {pending} confirmations waiting. Resolve those first.")
        cid = uuid.uuid4().hex[:10]
        t = now()
        rec = new_rec(cid, guild_id, {
            "plan": plan.to_dict(), "plan_hash": plan_hash(plan), "channel_id": channel_id, "request_id": request_id,
            "source": source, "request": request_text[:200],
        }, ts=iso(t), actor_id=requester_id, kind="plan", status="pending", ref_id=plan.plan_id,
            expires_at=iso(t + timedelta(seconds=c.ttl_seconds)))
        await self.app.store.put("confirmations", rec)
        await self.app.audit.record(guild_id=guild_id, action="confirmation.requested", actor_id=requester_id,
                                    request_id=request_id, plan_id=plan.plan_id, confirmation_status="pending",
                                    details={"confirmation": cid, "steps": [s.preview for s in plan.steps]})
        return rec

    async def decide(self, cid: str, user_id: int, guild_id: int, approve: bool) -> tuple[dict, BoundPlan | None]:
        store = self.app.store
        rec = await store.get("confirmations", cid, guild_id)
        if rec is None:
            raise PlanError("unknown confirmation", user_message="That confirmation doesn't exist any more.")
        base = dict(guild_id=guild_id, actor_id=user_id, request_id=rec["data"]["request_id"], plan_id=rec["ref_id"])
        if rec["status"] != "pending":
            raise PlanError("not pending", user_message=f"That confirmation was already {rec['status']}.")
        if rec["expires_at"] <= iso(now()):
            if await store.update_if("confirmations", cid, {"status": "pending"}, {"status": "expired"}, guild_id):
                await self.app.audit.record(**base, action="confirmation.expired", confirmation_status="expired")
            raise PlanError("expired", user_message="That confirmation expired. Ask me again if you still want it.")
        if user_id != rec["actor_id"]:
            await self.app.audit.record(**base, action="confirmation.denied", status="denied", authorization_result="denied",
                                        details={"reason": "not the requester", "confirmation": cid})
            raise Denied("not requester", user_message="Only the person who made the request can confirm it.")
        if not approve:
            if await store.update_if("confirmations", cid, {"status": "pending"}, {"status": "rejected"}, guild_id):
                await self.app.audit.record(**base, action="confirmation.rejected", confirmation_status="rejected")
            return rec, None
        plan = BoundPlan.from_dict(rec["data"]["plan"])
        if plan_hash(plan) != rec["data"]["plan_hash"]:
            await store.update_if("confirmations", cid, {"status": "pending"}, {"status": "rejected"}, guild_id)
            await self.app.audit.record(**base, action="confirmation.approved", status="failure", error_code="plan_tampered",
                                        confirmation_status="rejected")
            raise PlanError("hash mismatch", user_message="That plan changed after I showed it to you, so I discarded it.")
        if not await store.update_if("confirmations", cid, {"status": "pending"}, {"status": "approved"}, guild_id):
            raise PlanError("race", user_message="That confirmation was already handled.")
        await self.app.audit.record(**base, action="confirmation.approved", confirmation_status="approved",
                                    details={"confirmation": cid})
        return rec, plan

    async def mark_executed(self, cid: str, guild_id: int, ok: bool) -> None:
        await self.app.store.update_if("confirmations", cid, {"status": "approved"},
                                       {"status": "executed", "data": {"ok": ok}}, guild_id)
