from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class EntityInfo(BaseModel):
    entity_id: str
    friendly_name: str | None = None
    state: str
    last_changed: str | None = None
    unit_of_measurement: str | None = None
    device_class: str | None = None

    @classmethod
    def from_ha(cls, raw: dict[str, Any]) -> "EntityInfo":
        attrs = raw.get("attributes", {}) or {}
        return cls(
            entity_id=raw["entity_id"],
            friendly_name=attrs.get("friendly_name"),
            state=raw.get("state", "unknown"),
            last_changed=raw.get("last_changed"),
            unit_of_measurement=attrs.get("unit_of_measurement"),
            device_class=attrs.get("device_class"),
        )


class EntityState(BaseModel):
    entity_id: str
    state: str
    attributes: dict[str, Any] = Field(default_factory=dict)
    last_changed: str | None = None
    last_updated: str | None = None

    @classmethod
    def from_ha(cls, raw: dict[str, Any]) -> "EntityState":
        return cls(
            entity_id=raw["entity_id"],
            state=raw.get("state", "unknown"),
            attributes=raw.get("attributes", {}) or {},
            last_changed=raw.get("last_changed"),
            last_updated=raw.get("last_updated"),
        )


class HistoryPoint(BaseModel):
    state: str
    last_changed: str | None = None
    unit_of_measurement: str | None = None

    @classmethod
    def from_ha(cls, raw: dict[str, Any]) -> "HistoryPoint":
        attrs = raw.get("attributes", {}) or {}
        return cls(
            state=raw.get("state", "unknown"),
            last_changed=raw.get("last_changed"),
            unit_of_measurement=attrs.get("unit_of_measurement"),
        )


class BinnedPoint(BaseModel):
    bucket_start: str
    n: int
    mean: float | None = None
    min: float | None = None
    max: float | None = None
    first: float | None = None
    last: float | None = None
