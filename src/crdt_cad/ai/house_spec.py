"""The structured specification an architectural text prompt gets
reduced to, by either the LLM interpreter or its heuristic fallback."""

from __future__ import annotations

from pydantic import BaseModel, Field


class HouseSpec(BaseModel):
    bedrooms: int = Field(ge=1, le=12, default=1)
    floors: int = Field(ge=1, le=4, default=1)
    floor_material: str = "concrete"
    wall_height_m: float = Field(gt=0, le=6.0, default=2.7)
    style: str = "modern"
