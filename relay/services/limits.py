from __future__ import annotations

import time
from collections import defaultdict, deque

from relay.config import LimitsCfg
from relay.utils.errors import RateLimited


class Limiter:
    def __init__(self, cfg: LimitsCfg):
        self.cfg = cfg
        self._hits: dict[tuple, deque] = defaultdict(deque)
        self._active: dict[int, int] = defaultdict(int)

    def hit(self, key: tuple, limit: int, window: float = 60.0) -> float:
        now = time.monotonic()
        q = self._hits[key]
        while q and q[0] <= now - window:
            q.popleft()
        if len(q) >= limit:
            return max(0.1, q[0] + window - now)
        q.append(now)
        return 0.0

    def check_request(self, guild_id: int, user_id: int) -> None:
        w = self.hit(("user", guild_id, user_id), self.cfg.user_per_minute)
        if w:
            raise RateLimited("user rate limit", w)
        w = self.hit(("guild", guild_id), self.cfg.guild_per_minute)
        if w:
            raise RateLimited("guild rate limit", w)

    def check_ai(self, guild_id: int) -> None:
        w = self.hit(("ai", guild_id), self.cfg.ai_per_minute_per_guild)
        if w:
            raise RateLimited("ai rate limit", w)

    def check_history(self, guild_id: int) -> None:
        w = self.hit(("history", guild_id), self.cfg.history_searches_per_minute)
        if w:
            raise RateLimited("history search limit", w)

    def acquire_plan(self, guild_id: int) -> bool:
        if self._active[guild_id] >= self.cfg.concurrent_plans_per_guild:
            return False
        self._active[guild_id] += 1
        return True

    def release_plan(self, guild_id: int) -> None:
        self._active[guild_id] = max(0, self._active[guild_id] - 1)
