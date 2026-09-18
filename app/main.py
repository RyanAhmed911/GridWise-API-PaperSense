import logging

from dotenv import load_dotenv

load_dotenv()  # must run before app.interpreter reads GROQ_API_KEY / GROQ_MODEL

from fastapi import FastAPI, HTTPException

from app.interpreter import interpret_operator_notes
from app.models import (
    DirectiveInterpretation,
    HealthResponse,
    HourlyPlanEntry,
    OptimizeEnergyRequest,
    OptimizeEnergyResponse,
)
from app.optimizer import optimize_schedule

logger = logging.getLogger(__name__)

app = FastAPI(title="GridWise API")


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok")


def _build_plan_summary(validated_directives: list[dict], result: dict) -> str:
    applied_types = [d["directive_type"] for d in validated_directives if d["applies"]]
    cost = result["total_cost_bdt"]
    if applied_types:
        return f"Applied directives: {', '.join(applied_types)}; total grid cost {cost:.2f} BDT."
    return f"No operator directives applied; total grid cost {cost:.2f} BDT."


@app.post("/optimize-energy", response_model=OptimizeEnergyResponse)
def optimize_energy(request: OptimizeEnergyRequest) -> OptimizeEnergyResponse:
    validated_directives = interpret_operator_notes(request.operator_notes, request.battery.capacity_kwh)

    try:
        result = optimize_schedule(
            scenario_id=request.scenario_id,
            hours=request.hours,
            battery=request.battery,
            validated_directives=validated_directives,
        )
    except RuntimeError:
        logger.exception("Optimization failed for scenario_id=%s", request.scenario_id)
        raise HTTPException(status_code=500, detail="Optimization failed to find a valid schedule")

    return OptimizeEnergyResponse(
        scenario_id=request.scenario_id,
        directive_interpretation=[DirectiveInterpretation(**d) for d in validated_directives],
        hourly_plan=[HourlyPlanEntry(**entry) for entry in result["hourly_plan"]],
        total_grid_kwh=result["total_grid_kwh"],
        total_cost_bdt=result["total_cost_bdt"],
        peak_grid_kwh=result["peak_grid_kwh"],
        plan_summary=_build_plan_summary(validated_directives, result),
    )
