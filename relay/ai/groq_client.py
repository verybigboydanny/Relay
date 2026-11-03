from __future__ import annotations

import asyncio
import json
import logging
import random
import re

import groq
from groq import AsyncGroq

from relay.config import GroqCfg
from relay.utils.errors import AIError

log = logging.getLogger(__name__)


class GroqClient:
    def __init__(self, api_key: str, cfg: GroqCfg, client: AsyncGroq | None = None):
        self.cfg = cfg
        self.client = client or AsyncGroq(api_key=api_key, timeout=cfg.timeout_seconds, max_retries=0)
        self._json_ok = cfg.json_mode == "json_object"
        self._effort = cfg.reasoning_effort

    @property
    def model(self) -> str:
        return self.cfg.model

    async def _call(self, messages: list[dict], json_mode: bool, max_tokens: int | None = None) -> str:
        kw = dict(model=self.cfg.model, messages=messages, temperature=self.cfg.temperature,
                  max_tokens=max_tokens or self.cfg.max_tokens)
        if json_mode and self._json_ok:
            kw["response_format"] = {"type": "json_object"}
        if self._effort:
            kw["reasoning_effort"] = self._effort
        attempt = 0
        while True:
            try:
                resp = await self.client.chat.completions.create(**kw)
                text = resp.choices[0].message.content or ""
                if not text.strip():
                    raise AIError("empty response from model", user_message="The model returned nothing. Try again.")
                return text
            except groq.BadRequestError as e:
                if "reasoning_effort" in kw and "reasoning" in str(e).lower():
                    log.warning("model doesn't take reasoning_effort; continuing without it (%s)", e)
                    self._effort = None
                    kw.pop("reasoning_effort")
                    continue
                if "response_format" in kw and ("response_format" in str(e) or "json" in str(e).lower()):
                    log.warning("model rejected json mode; continuing without it (%s)", e)
                    self._json_ok = False
                    kw.pop("response_format")
                    continue
                raise AIError(f"bad request to Groq: {e}", user_message="The AI service rejected that request.")
            except groq.AuthenticationError:
                raise AIError("Groq authentication failed", user_message="My AI credentials are invalid. An admin needs to check the Groq key.")
            except groq.NotFoundError:
                raise AIError(f"model {self.cfg.model} not found",
                              user_message=f"The configured model ({self.cfg.model}) isn't available. An admin needs to update the config.")
            except (groq.RateLimitError, groq.InternalServerError, groq.APIConnectionError, groq.APITimeoutError) as e:
                attempt += 1
                if attempt > self.cfg.max_retries:
                    raise AIError(f"Groq unavailable: {type(e).__name__}", user_message="The AI service is busy or unreachable right now. Try again in a moment.")
                delay = min(2 ** attempt + random.random(), 10)
                resp_h = getattr(getattr(e, "response", None), "headers", None)
                if resp_h and resp_h.get("retry-after"):
                    try:
                        delay = min(float(resp_h["retry-after"]), 15)
                    except ValueError:
                        pass
                await asyncio.sleep(delay)
            except groq.APIStatusError as e:
                raise AIError(f"Groq error {e.status_code}", user_message="The AI service returned an error.")

    async def chat_json(self, messages: list[dict]) -> str:
        return await self._call(messages, True)

    async def chat_text(self, messages: list[dict], max_tokens: int = 700) -> str:
        return await self._call(messages, False, max_tokens)

    async def ping(self) -> dict:
        try:
            ids = [m.id for m in (await self.client.models.list()).data]
        except Exception as e:
            return {"ok": False, "error": type(e).__name__}
        return {"ok": True, "model": self.cfg.model, "model_available": self.cfg.model in ids, "json_mode": self._json_ok,
                    "suggestions": [] if self.cfg.model in ids else [i for i in ids if "gpt-oss" in i or "qwen" in i][:6]}


def extract_json(text: str) -> dict:
    t = text.strip()
    t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.M).strip()
    try:
        out = json.loads(t)
    except json.JSONDecodeError:
        a, b = t.find("{"), t.rfind("}")
        if a < 0 or b <= a:
            raise ValueError("no JSON object found")
        out = json.loads(t[a:b + 1])
    if not isinstance(out, dict):
        raise ValueError("expected a JSON object")
    return out
