# abqbatch

`abqbatch` is a Python 3.10+ command line tool for engineering-style batch Abaqus
`.inp` workflows: inventory raw inputs, validate them, generate isolated derived inputs,
run datacheck/solve through a runner abstraction, resume interrupted batches, classify
failures, postprocess ODB files, and generate reports.

The project is intentionally not a one-off script. It keeps raw `.inp` files read-only,
stores all state in SQLite, writes every Abaqus command to `command.txt`, and keeps each
case in `work/cases/<case_id>/`.

## Why Raw INP Files Are Never Edited

Raw inputs are the provenance anchor for a simulation batch. Editing them in place makes
it hard to reproduce a failed run, compare material/load variants, or prove which input
created an ODB. `abqbatch` copies each raw input into a case work directory and writes a
derived `generated.inp` plus include files such as `material.inc`, `load.inc`,
`restart.inc`, and `output.inc`.

## Install

```bash
pip install -e ".[dev]"
```

## Quick Start

```bash
abqbatch init --path examples/basic_project
abqbatch inventory --project examples/basic_project --input raw_inp
abqbatch validate --project examples/basic_project --static
abqbatch generate --project examples/basic_project
abqbatch datacheck --project examples/basic_project --dry-run
abqbatch run --project examples/basic_project --dry-run
abqbatch post --project examples/basic_project --dry-run
abqbatch report --project examples/basic_project
abqbatch status --project examples/basic_project
```

Dry-run commands use a fake runner and do not call real Abaqus.

## Directory Layout

- `configs/`: project, cases, material, load, and postprocess YAML/CSV configs.
- `raw_inp/`: immutable source Abaqus inputs.
- `work/cases/<case_id>/`: generated inputs, attempts, logs, ODBs, post outputs.
- `db/state.sqlite`: pipeline state, attempts, artifacts, and scalar results.
- `reports/`: validation and batch summaries.

## Configuration

`project.yml` defines Abaqus command defaults, runner behavior, paths, and generation
options. `cases.csv` maps each `case_id` to a source input and selected material/load/post
profiles. `materials.yml`, `load_schemes.yml`, and `postprocess.yml` define reusable
profiles.

## State Machine

Normal progression:

```text
DISCOVERED -> STATIC_CHECKED -> GENERATED -> DATACHECKED -> QUEUED -> RUNNING -> SOLVED -> POST_DONE
```

Failure states include `STATIC_FAILED`, `DATACHECK_FAILED`, `SOLVE_FAILED`,
`POST_FAILED`, `INTERRUPTED`, `RESTART_FAILED`, and `RECOVER_FAILED`. `POST_FAILED`
is retried only at postprocessing; it does not trigger a rerun of the solve.

## Restart And Recover

`abqbatch restart --project . --case C000001` records a Standard restart command:

```text
abaqus job=C000001_restart_001 oldjob=C000001 interactive
```

`abqbatch recover --project . --case C000001` records an Explicit recover command:

```text
abaqus job=C000001 recover interactive
```

The tool does not automatically assume convergence or numerical failures are safe to
restart.

## Postprocessing

The main controller converts `postprocess.yml` profiles into JSON and invokes the isolated
Abaqus Python script:

```text
abaqus python post_odb.py --odb <odb> --spec <post_spec.json> --out <post_dir>
```

The script uses only the standard library and Abaqus `odbAccess`. In normal Python it
writes a clear `post.log` explaining that `odbAccess` is unavailable.

## CI

GitHub Actions runs:

```bash
ruff check .
pytest
```

CI never requires Abaqus. Tests use `FakeRunner` or dry-run paths.

## Current Limits

- The INP parser is a reliable keyword-block parser, not a full Abaqus parser.
- CI does not run real Abaqus jobs.
- ODB postprocessing must run in an Abaqus Python environment.
- Complex user-subroutine compilation must be configured by the user.

## Roadmap

- SLURM/PBS/LSF adapters.
- Parameter sweep expansion.
- Parquet output.
- Web dashboard.
- More complete Abaqus keyword parsing.
