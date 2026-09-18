# GridWise API

LLM-assisted 24-hour campus energy scheduler for the BUP CSE Fest 2026 online
preliminary. `POST /optimize-energy` interprets 1-3 natural-language operator
notes with an LLM, deterministically guardrails that interpretation, applies
it to a linear-programming schedule, and returns a validated, cost-minimized
24-hour plan.

## Architecture overview

```
POST /optimize-energy
        |
        v
Pydantic request validation (app/models.py)
        |
        v
LLM interpretation (app/interpreter.py, Groq)
   - one structured directive per operator note
        |
        v
Deterministic guardrail validator (app/interpreter.py: validate_directives)
   - checks directive_type, hours, numeric ranges, applies/no_op semantics
   - downgrades anything suspicious to a safe no_op instead of crashing
        |
        v
PuLP linear-programming optimizer (app/optimizer.py)
   - applies guardrailed directives to demand/solar/battery parameters
   - minimizes sum(grid_kwh * tariff_bdt_per_kwh) over 24 hours
        |
        v
Pydantic response validation -> JSON response
```

The LLM is only ever used to interpret `operator_notes` into structured
directives. It never touches the optimization math, and its raw output is
never trusted until `validate_directives` has checked it.

## Model / provider

- Provider: [Groq](https://groq.com/) (`groq` Python SDK).
- Model: `qwen/qwen3.8-27b` by default. This is overridable via the
  `GROQ_MODEL` environment variable if Groq retires or renames it before
  judging — check `client.models.list()` on the account's API key for
  currently available model IDs.

## Environment variables

| Variable | Required | Meaning |
|---|---|---|
| `GROQ_API_KEY` | Yes | Groq API key used for operator-note interpretation. |
| `GROQ_MODEL` | No | Overrides the default Groq model (`qwen/qwen3.8-27b`). |

Set these in a local `.env` file (already covered by `.gitignore`, never
committed) or as real environment variables / container `-e` flags. No
secret value is committed anywhere in this repository, and no secret is
baked into the Docker image.

## Endpoints

### `GET /health`

```bash
curl http://127.0.0.1:8000/health
# {"status":"ok"}
```

### `POST /optimize-energy`

```bash
curl -X POST http://127.0.0.1:8000/optimize-energy \
  -H "Content-Type: application/json" \
  -d '{
    "scenario_id": "DEMO-1",
    "operator_notes": ["Solar output will drop to about 20% from 1 PM to 3 PM."],
    "hours": [ /* 24 entries: hour, demand_kwh, solar_kwh, tariff_bdt_per_kwh */ ],
    "battery": {
      "capacity_kwh": 500,
      "initial_energy_kwh": 200,
      "minimum_energy_kwh": 50,
      "max_charge_kwh_per_hour": 100,
      "max_discharge_kwh_per_hour": 100
    }
  }'
```

See `BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json` for complete, ready-to-send
request bodies.

## Local quickstart (clean environment)

```bash
git clone <this-repository-url> gridwise-api
cd gridwise-api
python -m venv .venv
```

Activate the virtual environment, then install dependencies:

```bash
# Windows (Git Bash)
source .venv/Scripts/activate
# macOS/Linux
source .venv/bin/activate

pip install -r requirements.txt
```

Create a `.env` file in the project root (never commit this file):

```
GROQ_API_KEY=your_groq_api_key_here
```

Start the server:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Verify readiness:

```bash
curl http://127.0.0.1:8000/health
```

Run the full local test suite against the public sample cases (asserts
`directive_interpretation` and `total_cost_bdt` match the expected values
within 0.01 tolerance for all 10 cases):

```bash
python test_pipeline.py
```

Expected output ends with `10/10 sample cases passed.` and exit code 0.

## Docker

Build:

```bash
docker build -t gridwise-api .
```

Run (the key is supplied at runtime, never baked into the image):

```bash
docker run --rm -p 8000:8000 -e GROQ_API_KEY=your_groq_api_key_here gridwise-api
```

Verify:

```bash
curl http://127.0.0.1:8000/health
```

The image installs `libgomp1`, which PuLP's bundled CBC solver binary needs
on Debian-based images.

## Project structure

```
app/
  main.py         FastAPI app: GET /health, POST /optimize-energy
  models.py       Pydantic request/response schemas (Problem Statement Sec. 07, 10)
  interpreter.py  Groq LLM call + deterministic guardrail validator (Sec. 04, 08)
  optimizer.py    PuLP 24-hour LP scheduler (Sec. 05.3, 09)
test_pipeline.py  Integration test against the 10 public sample cases
Dockerfile
requirements.txt
```

## Guardrails

`validate_directives` in `app/interpreter.py` never trusts raw LLM output.
For every operator note it checks: exactly one mapped interpretation per
note index, `directive_type` is one of the six supported types, `hours` are
unique ascending integers in `[0, 23]`, `solar_reduction.factor` is in
`[0, 1]`, reserve/grid-cap values are finite and non-negative, and
`applies`/`structured_adjustment` match the required `no_op` vs. non-`no_op`
shape. Anything that fails is downgraded to a safe `no_op` rather than
raising, so a bad or missing LLM generation can never crash the request.
The battery's `capacity_kwh` is given to the LLM as read-only reference
context so it can convert percentage-of-capacity reserve notes (e.g. "keep
50% of capacity") into an absolute kWh value before the guardrail even runs.

## Dependencies / acknowledgments

- [FastAPI](https://fastapi.tiangolo.com/) + [Uvicorn](https://www.uvicorn.org/) - HTTP API and ASGI server.
- [Pydantic](https://docs.pydantic.dev/) - request/response schema validation.
- [Groq](https://groq.com/) Python SDK - LLM operator-note interpretation.
- [PuLP](https://coin-or.github.io/pulp/) (bundled CBC solver) - 24-hour linear-programming optimizer.
- [python-dotenv](https://pypi.org/project/python-dotenv/) - loads `.env` for local development.

## Known limitations

- The LLM call depends on Groq's hosted availability, quota, and latency.
  A provider outage or malformed generation degrades to a safe all-`no_op`
  interpretation rather than crashing, but that also means directive
  application is skipped for that request until Groq recovers.
- Under rapid repeated requests we occasionally observed a single transient
  failure from the Groq API (timeout/rate limit) during local testing; this
  is an external dependency characteristic, not a guardrail or optimizer
  bug. Retrying the request succeeds.
- The optimizer assumes the organizer's guarantee that valid scoring
  scenarios are feasible; a genuinely infeasible combination of directives
  returns a controlled HTTP 500 rather than a schedule.
- `minimum_battery_reserve` percentage-to-kWh conversion relies on the LLM
  correctly using the battery-capacity context provided in the prompt; it
  is not independently re-derived by the deterministic guardrail (which,
  per spec, receives only the interpretations and note count).
