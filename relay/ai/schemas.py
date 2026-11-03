from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

STEP_ID = re.compile(r"^s\d{1,2}$")


class EverySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    freq: Literal["interval", "daily", "weekly", "monthly"]
    interval: str | None = None
    time: str | None = None
    weekday: str | list[str] | None = None
    day: int | None = None

    @model_validator(mode="after")
    def _need(self):
        if self.freq == "interval" and not self.interval:
            raise ValueError("interval recurrence needs 'interval'")
        return self


class When(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    in_: str | None = Field(None, alias="in")
    at: str | None = None
    every: EverySpec | None = None

    @model_validator(mode="after")
    def _one(self):
        if sum(x is not None for x in (self.in_, self.at, self.every)) != 1:
            raise ValueError("'when' needs exactly one of in / at / every")
        return self


class StepIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    when: When | None = None

    @model_validator(mode="after")
    def _id(self):
        if not STEP_ID.match(self.id):
            raise ValueError(f"bad step id '{self.id}'")
        return self


class PlannerOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    type: Literal["plan", "reply", "clarify"]
    steps: list[StepIn] = Field(default_factory=list)
    text: str | None = None
    note: str | None = None

    @model_validator(mode="after")
    def _shape(self):
        if self.type == "plan":
            if not self.steps:
                raise ValueError("a plan needs at least one step")
            ids = [s.id for s in self.steps]
            if len(set(ids)) != len(ids):
                raise ValueError("duplicate step ids")
        elif not (self.text or "").strip():
            raise ValueError(f"'{self.type}' needs text")
        return self
