"""24-hour LP scheduler (Problem Statement Section 05.3 and Section 09)."""
from __future__ import annotations

import pulp

from app.models import BatterySpec, DirectiveType, HourEntry

_EPS = 1e-6


def _apply_directives(
    base_solar: dict[int, float],
    base_min_reserve: float,
    validated_directives: list[dict],
) -> tuple[dict[int, float], dict[int, float], set[int], set[int], dict[int, float]]:
    effective_solar = dict(base_solar)
    min_reserve = {h: base_min_reserve for h in range(24)}
    no_charge_hours: set[int] = set()
    no_discharge_hours: set[int] = set()
    max_grid: dict[int, float] = {}

    for directive in validated_directives:
        if not directive.get("applies"):
            continue
        directive_type = directive.get("directive_type")
        adjustment = directive.get("structured_adjustment") or {}
        hours = adjustment.get("hours", [])

        if directive_type == DirectiveType.solar_reduction.value:
            factor = adjustment["factor"]
            for h in hours:
                effective_solar[h] = effective_solar[h] * factor
        elif directive_type == DirectiveType.minimum_battery_reserve.value:
            reserve = adjustment["minimum_energy_kwh"]
            for h in hours:
                min_reserve[h] = max(min_reserve[h], reserve)
        elif directive_type == DirectiveType.no_charge_window.value:
            no_charge_hours.update(hours)
        elif directive_type == DirectiveType.no_discharge_window.value:
            no_discharge_hours.update(hours)
        elif directive_type == DirectiveType.max_grid_window.value:
            cap = adjustment["max_grid_kwh"]
            for h in hours:
                max_grid[h] = min(max_grid.get(h, cap), cap)

    return effective_solar, min_reserve, no_charge_hours, no_discharge_hours, max_grid


def optimize_schedule(
    scenario_id: str,
    hours: list[HourEntry],
    battery: BatterySpec,
    validated_directives: list[dict],
) -> dict:
    """Solve the 24-hour cost-minimizing schedule under the applied directives.

    Returns a dict with hourly_plan (list of 24 dicts matching the
    HourlyPlanEntry schema) plus total_grid_kwh, total_cost_bdt, and
    peak_grid_kwh recalculated from that plan.
    """
    hours_sorted = sorted(hours, key=lambda h: h.hour)
    base_solar = {h.hour: h.solar_kwh for h in hours_sorted}
    demand = {h.hour: h.demand_kwh for h in hours_sorted}
    tariff = {h.hour: h.tariff_bdt_per_kwh for h in hours_sorted}

    effective_solar, min_reserve, no_charge_hours, no_discharge_hours, max_grid = _apply_directives(
        base_solar, battery.minimum_energy_kwh, validated_directives
    )

    hour_range = range(24)
    problem = pulp.LpProblem("gridwise_schedule", pulp.LpMinimize)

    grid = {h: pulp.LpVariable(f"grid_{h}", lowBound=0) for h in hour_range}
    solar_used = {h: pulp.LpVariable(f"solar_used_{h}", lowBound=0) for h in hour_range}
    charge = {h: pulp.LpVariable(f"charge_{h}", lowBound=0, upBound=battery.max_charge_kwh_per_hour) for h in hour_range}
    discharge = {h: pulp.LpVariable(f"discharge_{h}", lowBound=0, upBound=battery.max_discharge_kwh_per_hour) for h in hour_range}
    energy_after = {h: pulp.LpVariable(f"energy_after_{h}", lowBound=0, upBound=battery.capacity_kwh) for h in hour_range}

    for h in hour_range:
        problem += solar_used[h] <= effective_solar[h]
        problem += grid[h] + solar_used[h] + discharge[h] == demand[h] + charge[h]
        problem += energy_after[h] >= min_reserve[h]

        prev_energy = battery.initial_energy_kwh if h == 0 else energy_after[h - 1]
        problem += energy_after[h] == prev_energy + charge[h] - discharge[h]

        if h in no_charge_hours:
            problem += charge[h] == 0
        if h in no_discharge_hours:
            problem += discharge[h] == 0
        if h in max_grid:
            problem += grid[h] <= max_grid[h]

    problem += energy_after[23] == battery.initial_energy_kwh

    problem += pulp.lpSum(grid[h] * tariff[h] for h in hour_range)

    status = problem.solve(pulp.PULP_CBC_CMD(msg=False))
    if pulp.LpStatus[status] != "Optimal":
        raise RuntimeError(f"Optimizer did not reach an optimal solution: {pulp.LpStatus[status]}")

    hourly_plan = []
    for h in hour_range:
        charge_val = max(charge[h].value() or 0.0, 0.0)
        discharge_val = max(discharge[h].value() or 0.0, 0.0)

        # An LP has no incentive to avoid simultaneous charge+discharge since
        # only their difference affects energy balance and state transition;
        # cancel any overlap so battery_action stays a single unambiguous value.
        overlap = min(charge_val, discharge_val)
        charge_val -= overlap
        discharge_val -= overlap

        if charge_val > _EPS:
            action, battery_kwh = "charge", charge_val
        elif discharge_val > _EPS:
            action, battery_kwh = "discharge", discharge_val
        else:
            action, battery_kwh = "idle", 0.0

        hourly_plan.append(
            {
                "hour": h,
                "grid_kwh": max(grid[h].value() or 0.0, 0.0),
                "solar_used_kwh": max(solar_used[h].value() or 0.0, 0.0),
                "battery_action": action,
                "battery_kwh": battery_kwh,
                "battery_energy_after_kwh": max(energy_after[h].value() or 0.0, 0.0),
            }
        )

    total_grid_kwh = sum(entry["grid_kwh"] for entry in hourly_plan)
    total_cost_bdt = sum(entry["grid_kwh"] * tariff[entry["hour"]] for entry in hourly_plan)
    peak_grid_kwh = max(entry["grid_kwh"] for entry in hourly_plan)

    return {
        "scenario_id": scenario_id,
        "hourly_plan": hourly_plan,
        "total_grid_kwh": total_grid_kwh,
        "total_cost_bdt": total_cost_bdt,
        "peak_grid_kwh": peak_grid_kwh,
    }
