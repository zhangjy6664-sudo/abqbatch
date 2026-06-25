# Restart And Recover

Batch resume and Abaqus restart are different operations.

Batch resume skips completed pipeline stages and continues from SQLite state. Abaqus
restart/recover creates Abaqus commands that continue a previous analysis job.

## Standard Restart

```text
abaqus job=<new_job> oldjob=<old_job> interactive
```

`ensure_restart_write()` inserts:

```text
*Restart, write, frequency=10
```

when a generated input has no restart-write request.

## Explicit Recover

```text
abaqus job=<old_job> recover interactive
```

## Failure Policy

`TIME_LIMIT` and `INTERRUPTED` are generally restart/recover candidates.
`CONVERGENCE_ERROR` and `NUMERICAL_ERROR` are not automatically restarted because they
usually require model or load changes.
