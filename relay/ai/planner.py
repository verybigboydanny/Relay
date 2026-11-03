from __future__ import annotations

import json
import logging

from pydantic import ValidationError

from relay.ai import prompts
from relay.ai.groq_client import GroqClient, extract_json
from relay.ai.schemas import PlannerOut
from relay.config import Config
from relay.tools.base import Registry
from relay.utils.errors import AIError

log = logging.getLogger(__name__)


class Planner:
    def __init__(self, cfg: Config, groq: GroqClient, registry: Registry):
        self.cfg = cfg
        self.groq = groq
        self.registry = registry

    def system(self, **kw) -> str:
        return prompts.planner_system(name=self.cfg.bot.name, personality=prompts.personality_block(self.cfg.personality, self.cfg.bot.name),
                                      catalog=self.registry.catalog(), **kw)

    async def plan(self, system: str, request: str, *, prior_error: str | None = None, prior_raw: str | None = None) -> PlannerOut:
        msgs = [{"role": "system", "content": system}, {"role": "user", "content": f"<request>\n{request}\n</request>"}]
        if prior_error:
            msgs += [{"role": "assistant", "content": prior_raw or "{}"},
                     {"role": "user", "content": prompts.REPAIR.format(err=prior_error[:400])}]
        raw = await self.groq.chat_json(msgs)
        try:
            return PlannerOut.model_validate(extract_json(raw))
        except (ValueError, ValidationError) as e:
            if prior_error:
                raise AIError(f"planner output invalid twice: {e}",
                              user_message="I couldn't turn that into a valid plan. Try rephrasing it.")
            return await self.plan(system, request, prior_error=_short(e), prior_raw=raw[:3000])

    async def repair(self, system: str, request: str, raw_plan: PlannerOut, error: str) -> PlannerOut:
        return await self.plan(system, request, prior_error=error, prior_raw=raw_plan.model_dump_json(by_alias=True, exclude_none=True))

    async def compose(self, question: str, records: list[str]) -> str:
        p = self.cfg.personality
        body = "\n".join(prompts.clean(r, 500) for r in records[:80]) or "(no records)"
        msgs = [
            {"role": "system", "content": prompts.compose_system(self.cfg.bot.name, prompts.personality_block(p, self.cfg.bot.name))},
            {"role": "user", "content": f"Question: {prompts.clean(question, 500)}\n\n<untrusted>\n{body}\n</untrusted>"},
        ]
        return (await self.groq.chat_text(msgs)).strip()[:1900]


def _short(e: Exception) -> str:
    if isinstance(e, ValidationError):
        return "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()[:4])
    return str(e)
