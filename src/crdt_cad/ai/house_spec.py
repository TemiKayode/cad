"""The structured specification an architectural text prompt gets
reduced to, by either the LLM interpreter or its heuristic fallback."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator


class HouseSpec(BaseModel):
    bedrooms: int = Field(ge=1, le=12, default=1)
    floors: int = Field(ge=1, le=4, default=1)
    floor_material: str = "concrete"
    wall_height_m: float = Field(gt=0, le=6.0, default=2.7)
    style: str = "modern"

    # Phase G1 enrichment -- roof/garage/materials/dimensioning/openings.
    roof_type: Literal["flat", "gable", "hip"] = "flat"
    garage: bool = False
    wall_material: str = "exterior_wall"
    roof_material: str = "roof"
    # Overrides the uniform `bedrooms`-derived room grid per floor, so
    # e.g. a ground floor can be larger than the floor above it -- "stories
    # with distinct footprints". Length must equal `floors` if given.
    bedrooms_per_floor: Optional[list[int]] = None
    # When set, the room grid is scaled (uniformly in X/Z) so the ground
    # floor's footprint area matches this target -- "a 30 square meter
    # cabin" must be honored, not just bedroom count.
    floor_area_sq_m: Optional[float] = Field(default=None, gt=1.0, le=2000.0)
    # Real CSG-cut openings (the door/window generator) on the ground
    # floor's front (south) exterior wall specifically -- not yet every
    # wall on every floor, documented honestly rather than accepted and
    # silently ignored (see build_house_mesh's docstring for the exact
    # scope and why). Both default off/zero so a plain `HouseSpec()`
    # keeps producing byte-for-byte the same mesh as before this
    # enrichment -- every existing test in test_procedural_house.py
    # still holds unchanged; these are opt-in additions, not a change to
    # what "the default house" means.
    front_door: bool = False
    front_windows: int = Field(ge=0, le=6, default=0)

    @model_validator(mode="after")
    def _bedrooms_per_floor_matches_floor_count(self) -> "HouseSpec":
        if self.bedrooms_per_floor is not None and len(self.bedrooms_per_floor) != self.floors:
            raise ValueError(
                f"bedrooms_per_floor has {len(self.bedrooms_per_floor)} entries but floors={self.floors}"
            )
        return self
