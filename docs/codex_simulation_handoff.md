# Codex Simulation Handoff

This document is for a Codex instance on another Windows workstation taking over the Abaqus meso compression calibration work.

Last updated: 2026-07-06 05:20 Asia/Shanghai.

## Final Current State

- Repository branch: `codex/meso-compression-batch-pipeline`.
- Calibration output root: `D:\ZDYF_NBY\abqbatch\runs\compression_strength_calibration`.
- Final manifest: `runs\compression_strength_calibration\calibration_manifest.json`.
- Final workbook: `runs\compression_strength_calibration\calibration_summary.xlsx`.
- Final history CSV: `runs\compression_strength_calibration\calibration_history.csv`.
- Final next-candidate CSV: `runs\compression_strength_calibration\next_candidates.csv`.
- Final plan id: `calibration_20260704_batch_16_codex_review`.
- Final candidate count: `0`.
- Final active target count: `104`.
- Final simulation point count: `191`.
- Final decision counts:
  - `accepted`: `80`
  - `max_attempts_reached`: `13`
  - `model_review_required`: `11`

No additional Abaqus candidate should be started by the current adaptive rules. Remaining cases are either accepted or explicitly marked for model review / max attempts.

## Latest Completed Batches

The final executed calibration batches were:

```text
calibration_20260704_batch_11_codex_review  rows=6  accepted=6
calibration_20260704_batch_12_codex_review  rows=6  accepted=4
calibration_20260704_batch_13_codex_review  rows=6  accepted=3
calibration_20260704_batch_14_codex_review  rows=6  accepted=3
calibration_20260704_batch_15_codex_review  rows=5  accepted=0
```

All `.odb` files under the final batch directories were deleted from D according to the user's later delete-no-archive policy. Each completed batch has an execution summary and an ODB deletion index under:

```text
runs\compression_strength_calibration\calibration_20260704_batch_*_codex_review_execution_summary.json
runs\compression_strength_calibration\archive_indexes\calibration_20260704_batch_*_codex_review_odb_deletion_index.csv
```

For batch10, final cleanup required manual recovery because the foreground runner timed out and a stale Abaqus process remained after early-stop. The recovery summary is:

```text
runs\compression_strength_calibration\calibration_20260703_batch_10_codex_review_execution_summary.json
```

## Data Source Contract

The calibration has strict source separation.

- Target strength source: `D:/ZDYF_NBY/\u9759\u5f3a\u5ea6\u6570\u636e\u96c6.xlsx`, Sheet1, column 5. This is `TARGET_ONLY`.
- Previous-round actual simulation reference: `D:/ZDYF_NBY/abqbatch/runs/puckzt_angle_0p8_g1c5_compression_all/reports/meso_compression_all_batches_summary.xlsx`. This is `REFERENCE_SIM`.
- Current calibration history: `D:/ZDYF_NBY/abqbatch/runs/compression_strength_calibration/calibration_history.csv`. This is `CURRENT_SIM`.
- The target workbook must never be used as an interpolation or trend source.
- Interpolation and trend review may only use `REFERENCE_SIM` and `CURRENT_SIM` records.
- Error and acceptance are computed against `TARGET_ONLY` only.
- Acceptance criterion: absolute relative strength error `<=15%`.

Excluded from continuing calibration:

```text
qj-24-24
13-24-24
13-24-48
13-24-72
```

Special source deck rule already applied:

```text
132-48-72 uses D:/ZDYF-NBY-ZJY/<data>/inp/inp-stat-JQ/inp-stat-JQ/132-48-72-jq.inp
```

## Advisor Rules Now Enforced In Code

The physical direction rules are hard constraints in `src/abqbatch/meso_compression.py`.

- If `sim > target`, the next move may only reduce strength:
  - first increase `angle` within `0-10 deg`;
  - only after angle is exhausted may G1C decrease;
  - never decrease angle;
  - never increase G1C.
- If `sim < target`, the next move may only increase strength:
  - first decrease `angle` within `0-10 deg`;
  - only after angle is exhausted may G1C increase;
  - never increase angle;
  - never decrease G1C.
- G1C stays fixed while a physically valid angle-only candidate exists.
- Each case gets at most one new attempt per batch.
- No automatic next-candidate generation happens after an executed batch unless `--auto-next-candidates` is explicitly used.
- The output records `selected_by=codex_advisor_review`, `advisor_rationale`, `decision_axis`, `physical_direction`, `monotonic_prior`, and `monotonic_violation`.

The known earlier wrong-direction cases are now marked for review instead of being continued:

```text
13-72-48: max_attempts_reached, includes historical angle/G1C direction violations
13-72-60: max_attempts_reached, includes historical angle direction violation
13-72-72: max_attempts_reached, includes historical angle direction violation
```

## Remaining Non-Accepted Outcomes

Final non-accepted cases are intentionally not assigned new candidates.

- `max_attempts_reached`: 13 cases, mostly old 13-* cases that used all three attempts.
- `model_review_required`: 11 QJ high-angle cases. These are `sim > target` but already at `angle=10` and `G1C=5`, so there is no remaining legal reduce-strength parameter move under the current bounds.

Inspect them in:

```text
runs\compression_strength_calibration\calibration_summary.xlsx
sheets: max_attempts, decisions
```

## Verification Commands

Run from `D:\ZDYF_NBY\abqbatch`.

```powershell
python -B -m ruff check src tests
python -B -m pytest tests\test_meso_compression.py -q
```

Expected at handoff:

```text
ruff: All checks passed
pytest: 22 passed
```

Check final decision counts:

```powershell
$script = @"
import json
from collections import Counter
from pathlib import Path
m=json.loads(Path('runs/compression_strength_calibration/calibration_manifest.json').read_text(encoding='utf-8'))
print({k:m.get(k) for k in ['batch_id','candidate_count','simulation_point_count','target_count','active_target_count']})
print(Counter(d.get('status') for d in m.get('decisions', [])))
"@
$script | python -B -
```

Expected:

```text
batch_id='calibration_20260704_batch_16_codex_review'
candidate_count=0
simulation_point_count=191
target_count=107
active_target_count=104
Counter({'accepted': 80, 'max_attempts_reached': 13, 'model_review_required': 11})
```

Check ODB cleanup:

```powershell
Get-ChildItem -Path runs\compression_strength_calibration\batches -Directory -Filter 'calibration_20260704_batch_*_codex_review' | ForEach-Object {
  $m = Get-ChildItem -LiteralPath $_.FullName -Recurse -Filter *.odb -File | Measure-Object -Property Length -Sum
  [PSCustomObject]@{Batch=$_.Name; OdbCount=$m.Count; OdbBytes=$m.Sum}
} | Format-Table -AutoSize
```

Expected for batch11 through batch15: `OdbCount=0`.

Check no Abaqus solver remains:

```powershell
Get-Process standard,pre,SMALauncher,SMAPython -ErrorAction SilentlyContinue
```

Expected: no output.

## If The User Asks To Continue Further

Do not generate opposite-direction fallback points. With the current rules, the final manifest has zero candidates. Further progress requires a model-review decision, for example:

- widen the angle range beyond `10 deg`;
- allow a lower G1C bound below `5`;
- change the material/UMAT model assumptions;
- revise target mapping or case exclusion rules.

Any such change is a new modeling decision and should be documented in `advisor_rationale` before running more Abaqus jobs.

## Git And Remote Notes

At this handoff, the branch has local commits ahead of `origin/codex/meso-compression-batch-pipeline`. Push with:

```powershell
git push origin codex/meso-compression-batch-pipeline
```

There may be unrelated generated run files in the worktree. Do not revert user or generated data. Stage code/docs intentionally.

## Hard Guardrails

- Do not use the target workbook as a simulation/reference point source.
- Do not call raw `abaqus`; use `C:\Users\11843\codex_abaqus.cmd` through the pipeline.
- Do not leave `.odb` files on D after a completed batch.
- Do not delete `.dat`, curve CSV, summary JSON, state JSON, manifest JSON, history CSV, archive indexes, or workbooks from D.
- Do not mark this work as complete unless every active case is accepted or has a documented max-attempt/model-review outcome, all ODB files are deleted or archived according to the current policy, and the summary workbook is current.
