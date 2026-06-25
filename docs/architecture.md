# Architecture

`abqbatch` separates source inputs, generated artifacts, execution attempts, and reports.
Raw `.inp` files under `raw_inp/` are never modified. Each case receives a stable
`case_id` and an isolated directory under `work/cases/<case_id>/`.

## Data Flow

```text
raw_inp -> inventory -> SQLite cases
cases + configs -> validate -> validation_report.csv
cases + materials/loads -> generate -> work/cases/<case_id>/input
generated.inp -> datacheck -> attempts/artifacts
datachecked cases -> run -> ODB/logs
solved cases -> post -> metrics/history/field CSV
SQLite + post outputs -> report -> reports/*.csv + HTML
```

## State Machine

Normal progression is:

```text
DISCOVERED -> STATIC_CHECKED -> GENERATED -> DATACHECKED -> QUEUED -> RUNNING -> SOLVED -> POST_DONE
```

Failed phases update the case to a phase-specific failure state. Resume logic skips
completed work and only retries failed stages when explicitly requested.

## SQLite Tables

- `cases`: current state and selected profiles.
- `attempts`: each datacheck, solve, restart, recover, or post attempt.
- `artifacts`: generated files, logs, command files, and hashes.
- `results`: scalar metrics extracted from postprocessing.

## Idempotency

Generation starts from the immutable source input each time, so repeated generation with
unchanged configs produces stable output hashes. Execution attempts are separate rows, and
generated artifacts are recorded with SHA-256 digests.
