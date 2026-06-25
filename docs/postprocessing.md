# Postprocessing

The controller reads `postprocess.yml`, selects the case profile, and writes
`post_spec.json` for Abaqus Python. It then runs:

```text
abaqus python post_odb.py --odb <odb> --spec <post_spec.json> --out <post_dir>
```

Outputs per case:

- `metrics.json`
- `history_curves.csv`
- `field_summary.csv`
- `post.log`

`post_odb.py` uses only Python's standard library plus Abaqus `odbAccess`. It supports
field reductions `max`, `min`, and `mean`, and common invariants such as `Mises`,
`MaxPrincipal`, `MinPrincipal`, and `Magnitude`.
