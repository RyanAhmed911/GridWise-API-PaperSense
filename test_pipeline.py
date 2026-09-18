"""Formal integration test: replays every public sample case against a live server.

Requires the GridWise API to already be running (see README for how to start it).

Usage:
    python test_pipeline.py
    GRIDWISE_BASE_URL=http://localhost:9000 python test_pipeline.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE_URL = os.environ.get("GRIDWISE_BASE_URL", "http://127.0.0.1:8000")
SAMPLE_CASES_PATH = Path(__file__).parent / "BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json"
TOLERANCE = 0.01


def load_cases() -> list[dict]:
    with open(SAMPLE_CASES_PATH, encoding="utf-8") as f:
        return json.load(f)["cases"]


def post_optimize_energy(payload: dict) -> tuple[int, dict | str]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{BASE_URL}/optimize-energy",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def assert_close(actual: float, expected: float, label: str) -> None:
    assert abs(actual - expected) <= TOLERANCE, (
        f"{label}: expected {expected}, got {actual} (tolerance {TOLERANCE})"
    )


def assert_directive_interpretation(actual_entries: list, expected_entries: list, scenario_id: str) -> None:
    assert len(actual_entries) == len(expected_entries), (
        f"{scenario_id}: expected {len(expected_entries)} directive_interpretation entries, "
        f"got {len(actual_entries)}"
    )
    by_index = {entry["note_index"]: entry for entry in actual_entries}

    for expected in expected_entries:
        note_index = expected["note_index"]
        assert note_index in by_index, (
            f"{scenario_id}: missing directive_interpretation for note_index {note_index}"
        )
        actual = by_index[note_index]

        assert actual["applies"] == expected["applies"], (
            f"{scenario_id} note {note_index}: applies mismatch "
            f"(expected {expected['applies']}, got {actual['applies']})"
        )
        assert actual["directive_type"] == expected["directive_type"], (
            f"{scenario_id} note {note_index}: directive_type mismatch "
            f"(expected {expected['directive_type']}, got {actual['directive_type']})"
        )

        expected_adj = expected.get("structured_adjustment")
        actual_adj = actual.get("structured_adjustment")

        if expected_adj is None:
            assert actual_adj is None, (
                f"{scenario_id} note {note_index}: expected null structured_adjustment, got {actual_adj}"
            )
            continue

        assert actual_adj is not None, (
            f"{scenario_id} note {note_index}: expected non-null structured_adjustment"
        )
        assert actual_adj.get("hours") == expected_adj.get("hours"), (
            f"{scenario_id} note {note_index}: hours mismatch "
            f"(expected {expected_adj.get('hours')}, got {actual_adj.get('hours')})"
        )
        for key in ("factor", "minimum_energy_kwh", "max_grid_kwh"):
            if key in expected_adj:
                assert key in actual_adj, (
                    f"{scenario_id} note {note_index}: missing '{key}' in structured_adjustment"
                )
                assert_close(
                    float(actual_adj[key]), float(expected_adj[key]),
                    f"{scenario_id} note {note_index} {key}",
                )


def run_case(case: dict) -> None:
    scenario_id = case["id"]
    status, body = post_optimize_energy(case["input"])
    assert status == 200, f"{scenario_id}: expected HTTP 200, got {status}: {body}"

    assert body["scenario_id"] == case["input"]["scenario_id"], (
        f"{scenario_id}: scenario_id echo mismatch"
    )
    assert_directive_interpretation(
        body["directive_interpretation"],
        case["expected_output"]["directive_interpretation"],
        scenario_id,
    )
    assert_close(
        body["total_cost_bdt"], case["expected_output"]["total_cost_bdt"],
        f"{scenario_id} total_cost_bdt",
    )


def main() -> int:
    cases = load_cases()
    failures: list[str] = []

    for case in cases:
        scenario_id = case["id"]
        try:
            run_case(case)
        except AssertionError as exc:
            failures.append(scenario_id)
            print(f"FAIL {scenario_id}: {exc}")
        except Exception as exc:  # network/parsing errors, etc.
            failures.append(scenario_id)
            print(f"ERROR {scenario_id}: {exc}")
        else:
            print(f"PASS {scenario_id}")

    print(f"\n{len(cases) - len(failures)}/{len(cases)} sample cases passed.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
