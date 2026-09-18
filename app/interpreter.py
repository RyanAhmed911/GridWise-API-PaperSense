"""LLM interpretation pipeline for operator_notes (Problem Statement Section 04 & 08).

Flow: call_llm_for_notes() asks Groq for a raw, untrusted JSON interpretation
of each note. validate_directives() then deterministically checks that raw
output and downgrades anything suspicious to a safe no_op, so a bad or
missing LLM generation can never crash the API or reach the optimizer.
"""
from __future__ import annotations

import json
import logging
import math
import os

from groq import Groq

from app.models import DirectiveType

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "qwen/qwen3.8-27b"
GROQ_MODEL = os.environ.get("GROQ_MODEL", DEFAULT_MODEL)

SYSTEM_PROMPT = """You interpret short natural-language campus operator notes about a 24-hour \
energy schedule (hours 0-23) for a system called GridWise.

For EACH operator note, decide whether it affects today's 24-hour energy schedule. If it does, \
map it to exactly ONE of these six directive types. If it does not (irrelevant / distractor), \
use no_op.

- solar_reduction: usable solar is reduced during specific hours.
  structured_adjustment = {"hours": [int, ...], "factor": number}
  factor is the USABLE FRACTION REMAINING. An 80% reduction means factor = 0.2.
- minimum_battery_reserve: battery energy must stay at or above a level during specific hours.
  structured_adjustment = {"hours": [int, ...], "minimum_energy_kwh": number}
  minimum_energy_kwh must be an ABSOLUTE kWh value. If the note states the reserve as a percentage
  or fraction of battery capacity (e.g. "keep at least 50% of capacity"), convert it to kWh using
  the battery capacity given below before writing minimum_energy_kwh. Never output a bare fraction.
- no_charge_window: battery charging is unavailable during specific hours.
  structured_adjustment = {"hours": [int, ...]}
- no_discharge_window: battery discharging is unavailable during specific hours.
  structured_adjustment = {"hours": [int, ...]}
- max_grid_window: grid import may not exceed a stated amount during specific hours.
  structured_adjustment = {"hours": [int, ...], "max_grid_kwh": number}
- no_op: the note does not affect today's energy schedule. structured_adjustment = null.

Rules:
- Every "hours" array must contain unique integers from 0 to 23 in ascending order.
- Time windows are start-inclusive, end-exclusive: "1 PM to 3 PM" means hours [13, 14].
- Never invent a directive type outside the six listed above.
- Never invent or change demand, tariff, or battery parameters.
- For no_op: "applies" must be false and "structured_adjustment" must be null.
- For every other directive: "applies" must be true.
- Return exactly one interpretation per input note, indexed by "note_index" (0-based, matching \
input order). Do not skip, merge, or duplicate notes.

Respond with ONLY a single JSON object of this exact shape, and nothing else (no markdown, no \
commentary):
{"interpretations": [{"note_index": 0, "applies": true, "directive_type": "solar_reduction", \
"structured_adjustment": {"hours": [13, 14], "factor": 0.2}, "explanation": "short reason"}, ...]}
"""

_client: Groq | None = None


def _get_client() -> Groq:
    global _client
    if _client is None:
        api_key = os.environ.get("GROQ_API_KEY")
        if not api_key:
            raise RuntimeError("GROQ_API_KEY is not set")
        _client = Groq(api_key=api_key)
    return _client


def _build_user_message(operator_notes: list[str], battery_capacity_kwh: float) -> str:
    lines = [
        "Interpret the following operator notes for a 24-hour energy schedule (hours 0-23).",
        f"Reference data (for unit conversion only, do not alter): battery capacity = "
        f"{battery_capacity_kwh} kWh.",
        "",
    ]
    for index, note in enumerate(operator_notes):
        lines.append(f"note_index {index}: {note}")
    return "\n".join(lines)


def call_llm_for_notes(operator_notes: list[str], battery_capacity_kwh: float) -> list:
    """Call Groq and return the raw, untrusted list of interpretation dicts.

    Raises on any provider/parsing failure; callers must not treat this
    output as safe until it passes validate_directives().
    """
    client = _get_client()
    response = client.chat.completions.create(
        model=GROQ_MODEL,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_message(operator_notes, battery_capacity_kwh)},
        ],
    )
    content = response.choices[0].message.content
    parsed = json.loads(content)
    interpretations = parsed.get("interpretations")
    if not isinstance(interpretations, list):
        raise ValueError("LLM response is missing an 'interpretations' array")
    return interpretations


# ---------------------------------------------------------------------------
# Guardrail validator (Problem Statement Section 08)
# ---------------------------------------------------------------------------

_REQUIRED_ADJUSTMENT_KEYS = {
    DirectiveType.solar_reduction: {"hours", "factor"},
    DirectiveType.minimum_battery_reserve: {"hours", "minimum_energy_kwh"},
    DirectiveType.no_charge_window: {"hours"},
    DirectiveType.no_discharge_window: {"hours"},
    DirectiveType.max_grid_window: {"hours", "max_grid_kwh"},
}


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _valid_hours(value) -> bool:
    if not isinstance(value, list) or not value:
        return False
    if any(isinstance(h, bool) for h in value):
        return False
    if any(isinstance(h, float) and not h.is_integer() for h in value):
        return False
    try:
        hours = [int(h) for h in value]
    except (TypeError, ValueError):
        return False
    if len(set(hours)) != len(hours):
        return False
    if hours != sorted(hours):
        return False
    if any(h < 0 or h > 23 for h in hours):
        return False
    return True


def _validate_adjustment_shape(directive_type: DirectiveType, adjustment) -> bool:
    if not isinstance(adjustment, dict):
        return False
    required_keys = _REQUIRED_ADJUSTMENT_KEYS[directive_type]
    if not required_keys.issubset(adjustment.keys()):
        return False
    if not _valid_hours(adjustment.get("hours")):
        return False
    if directive_type == DirectiveType.solar_reduction:
        factor = adjustment.get("factor")
        if not _is_number(factor) or not (0.0 <= float(factor) <= 1.0):
            return False
    elif directive_type == DirectiveType.minimum_battery_reserve:
        reserve = adjustment.get("minimum_energy_kwh")
        if not _is_number(reserve) or reserve < 0:
            return False
    elif directive_type == DirectiveType.max_grid_window:
        cap = adjustment.get("max_grid_kwh")
        if not _is_number(cap) or cap < 0:
            return False
    return True


def _fallback_no_op(note_index: int, reason: str = "Downgraded to no_op: failed guardrail validation.") -> dict:
    return {
        "note_index": note_index,
        "applies": False,
        "directive_type": DirectiveType.no_op.value,
        "structured_adjustment": None,
        "explanation": reason,
    }


def _validate_single(raw: dict, note_index: int) -> dict:
    try:
        directive_type = DirectiveType(raw.get("directive_type"))
    except ValueError:
        return _fallback_no_op(note_index)

    applies = raw.get("applies")
    explanation = raw.get("explanation")
    if not isinstance(explanation, str) or not explanation.strip():
        explanation = f"Interpretation for note {note_index}."

    if directive_type == DirectiveType.no_op:
        if applies is not False:
            return _fallback_no_op(note_index)
        return {
            "note_index": note_index,
            "applies": False,
            "directive_type": DirectiveType.no_op.value,
            "structured_adjustment": None,
            "explanation": explanation,
        }

    if applies is not True:
        return _fallback_no_op(note_index)

    adjustment = raw.get("structured_adjustment")
    if not _validate_adjustment_shape(directive_type, adjustment):
        return _fallback_no_op(note_index)

    cleaned = {"hours": [int(h) for h in adjustment["hours"]]}
    if directive_type == DirectiveType.solar_reduction:
        cleaned["factor"] = float(adjustment["factor"])
    elif directive_type == DirectiveType.minimum_battery_reserve:
        cleaned["minimum_energy_kwh"] = float(adjustment["minimum_energy_kwh"])
    elif directive_type == DirectiveType.max_grid_window:
        cleaned["max_grid_kwh"] = float(adjustment["max_grid_kwh"])

    return {
        "note_index": note_index,
        "applies": True,
        "directive_type": directive_type.value,
        "structured_adjustment": cleaned,
        "explanation": explanation,
    }


def validate_directives(interpretations: list, num_notes: int) -> list[dict]:
    """Deterministically validate raw LLM output into exactly num_notes safe entries.

    - Exactly one interpretation per note index 0..num_notes-1 (missing or
      unmappable entries are backfilled with no_op).
    - hours arrays must be unique, ascending integers in [0, 23].
    - solar_reduction factor must be in [0, 1]; reserve/grid-cap values must
      be finite and non-negative.
    - Any entry that fails a check is downgraded to no_op rather than
      raising, so the API never crashes on a bad generation.
    """
    slots: list[dict | None] = [None] * num_notes

    if isinstance(interpretations, list):
        for raw in interpretations:
            if not isinstance(raw, dict):
                continue
            note_index = raw.get("note_index")
            if isinstance(note_index, bool) or not isinstance(note_index, int):
                continue
            if not (0 <= note_index < num_notes):
                continue
            if slots[note_index] is not None:
                continue  # duplicate mapping; keep the first valid one
            slots[note_index] = _validate_single(raw, note_index)

    for index in range(num_notes):
        if slots[index] is None:
            slots[index] = _fallback_no_op(index, "Downgraded to no_op: missing or unmappable LLM output.")

    return slots  # type: ignore[return-value]


def interpret_operator_notes(operator_notes: list[str], battery_capacity_kwh: float) -> list[dict]:
    """Interpret operator_notes via Groq, then guardrail the result.

    battery_capacity_kwh is passed as reference context only, so the model
    can convert a percentage-of-capacity reserve note into an absolute kWh
    value; it must not be echoed back as a new base parameter.

    Never raises: any LLM/provider/parsing failure degrades to a safe
    all-no_op interpretation instead of crashing the request.
    """
    try:
        raw_interpretations = call_llm_for_notes(operator_notes, battery_capacity_kwh)
    except Exception:
        logger.exception("Groq interpretation call failed; falling back to no_op for all notes")
        raw_interpretations = []
    return validate_directives(raw_interpretations, len(operator_notes))
