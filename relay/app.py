from __future__ import annotations

import uuid

import discord

import relay.tools  # noqa: F401
from relay.ai.groq_client import GroqClient
from relay.ai.planner import Planner
from relay.config import Settings
from relay.services.audit_service import AuditService
from relay.services.authorization import Authorizer
from relay.services.binder import Binder
from relay.services.confirmation_service import ConfirmationService
from relay.services.executor import Executor
from relay.services.limits import Limiter
from relay.services.memory_service import MemoryService
from relay.services.scheduler import Scheduler
from relay.storage.base import Store
from relay.tools.base import REGISTRY, Ctx
from relay.utils import time_parser as tp


class App:
    def __init__(self, settings: Settings, store: Store, groq: GroqClient | None = None, bot=None):
        self.settings = settings
        self.cfg = settings.cfg
        self.store = store
        self.bot = bot
        self.registry = REGISTRY
        self.memory = MemoryService(store, self.cfg)
        self.audit = AuditService(store, self.cfg.audit)
        self.auth = Authorizer(self.cfg)
        self.limits = Limiter(self.cfg.limits)
        self.groq = groq or GroqClient(settings.secrets.groq_api_key.get_secret_value(), self.cfg.groq)
        self.planner = Planner(self.cfg, self.groq, self.registry)
        self.binder = Binder(self.cfg, self.registry)
        self.executor = Executor(self)
        self.confirmations = ConfirmationService(self)
        self.scheduler = Scheduler(self)
        from relay.assistant import Assistant
        self.assistant = Assistant(self)

    async def make_ctx(self, guild: discord.Guild, actor: discord.Member | None, actor_id: int, channel, source: str, *,
                       plan_id: str | None = None, task_id: str | None = None, actor_type: str = "user",
                       request_id: str | None = None, entities: dict | None = None, allowed_ids: set[int] | None = None,
                       confirmed: bool = False) -> Ctx:
        settings = await self.memory.settings(guild.id)
        tz = await self.memory.timezone_for(guild.id, actor_id)
        try:
            tp.tz_of(tz)
        except tp.TimeError:
            tz = self.cfg.timezone
        return Ctx(app=self, guild=guild, actor=actor, actor_id=actor_id, channel=channel, source=source,
                   request_id=request_id or uuid.uuid4().hex[:12], plan_id=plan_id or uuid.uuid4().hex[:12], settings=settings,
                   tz_name=tz, actor_type=actor_type, task_id=task_id, entities=dict(entities or {}),
                   allowed_ids=allowed_ids or set(), confirmed=confirmed)
