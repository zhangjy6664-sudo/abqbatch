# Workflow

The intended workflow is:

```text
init -> inventory -> validate -> generate -> datacheck -> run -> post -> report
```

`init` creates the directory and default config files. `inventory` scans `.inp` files,
assigns `case_id` values, writes `cases.csv`, and upserts SQLite rows. `validate` performs
static checks before costly execution. `generate` creates isolated case inputs. `datacheck`
and `run` call Abaqus only through the runner abstraction. `post` extracts ODB metrics.
`report` writes CSV and HTML summaries.

Dry-run can be used for datacheck, run, and post demos without Abaqus.
