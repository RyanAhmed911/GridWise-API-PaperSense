"""Pydantic request/response schemas for the GridWise /optimize-energy contract.

Field names and shapes follow the Problem Statement:
  - Section 07: Request Schema
  - Section 04: Supported directive types / structured_adjustment shapes
  - Section 10: Response Schema
"""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Enums (Section 04, Section 10)
# ---------------------------------------------------------------------------

class DirectiveType(str, Enum):
    solar_reduction = "solar_reduction"
    minimum_battery_reserve = "minimum_battery_reserve"
    no_charge_window = "no_charge_window"
    no_discharge_window = "no_discharge_window"
    max_grid_window = "max_grid_window"
    no_op = "no_op"


class BatteryAction(str, Enum):
    charge = "charge"
    discharge = "discharge"
    idle = "idle"


# ---------------------------------------------------------------------------
# Request schema (Section 07)
# ---------------------------------------------------------------------------

class HourEntry(BaseModel):
    hour: int = Field(ge=0, le=23)
    demand_kwh: float = Field(ge=0)
    solar_kwh: float = Field(ge=0)
    tariff_bdt_per_kwh: float = Field(ge=0)


class BatterySpec(BaseModel):
    capacity_kwh: float = Field(gt=0)
    initial_energy_kwh: float = Field(ge=0)
    minimum_energy_kwh: float = Field(ge=0)
    max_charge_kwh_per_hour: float = Field(ge=0)
    max_discharge_kwh_per_hour: float = Field(ge=0)


class OptimizeEnergyRequest(BaseModel):
    scenario_id: str = Field(min_length=1)
    operator_notes: list[str] = Field(min_length=1, max_length=3)
    hours: list[HourEntry] = Field(min_length=24, max_length=24)
    battery: BatterySpec

    @field_validator("operator_notes")
    @classmethod
    def notes_must_be_non_empty(cls, notes: list[str]) -> list[str]:
        for note in notes:
            if not note.strip():
                raise ValueError("operator_notes entries must be non-empty strings")
        return notes

    @field_validator("hours")
    @classmethod
    def hours_must_cover_0_to_23(cls, hours: list[HourEntry]) -> list[HourEntry]:
        seen = sorted(h.hour for h in hours)
        if seen != list(range(24)):
            raise ValueError("hours must contain exactly one entry for each hour 0 through 23")
        return hours


# ---------------------------------------------------------------------------
# Response schema (Section 10)
# ---------------------------------------------------------------------------

class HealthResponse(BaseModel):
    status: str = "ok"


class DirectiveInterpretation(BaseModel):
    note_index: int = Field(ge=0)
    applies: bool
    directive_type: DirectiveType
    structured_adjustment: dict[str, Any] | None = None
    explanation: str

    @model_validator(mode="after")
    def no_op_requires_null_adjustment(self) -> "DirectiveInterpretation":
        if self.directive_type == DirectiveType.no_op:
            if self.applies is not False or self.structured_adjustment is not None:
                raise ValueError("no_op requires applies=false and structured_adjustment=null")
        elif self.applies is not True:
            raise ValueError("every non-no_op directive requires applies=true")
        return self


class HourlyPlanEntry(BaseModel):
    hour: int = Field(ge=0, le=23)
    grid_kwh: float = Field(ge=0)
    solar_used_kwh: float = Field(ge=0)
    battery_action: BatteryAction
    battery_kwh: float = Field(ge=0)
    battery_energy_after_kwh: float = Field(ge=0)

    @model_validator(mode="after")
    def idle_means_zero_battery_kwh(self) -> "HourlyPlanEntry":
        if self.battery_action == BatteryAction.idle and self.battery_kwh != 0:
            raise ValueError("battery_kwh must be 0 when battery_action is idle")
        return self


class OptimizeEnergyResponse(BaseModel):
    scenario_id: str
    directive_interpretation: list[DirectiveInterpretation]
    hourly_plan: list[HourlyPlanEntry] = Field(min_length=24, max_length=24)
    total_grid_kwh: float = Field(ge=0)
    total_cost_bdt: float = Field(ge=0)
    peak_grid_kwh: float = Field(ge=0)
    plan_summary: str
