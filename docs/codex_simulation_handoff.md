# Codex Simulation Handoff

This document is for a Codex instance on another Windows workstation taking over the Abaqus meso compression calibration work.

Last updated: 2026-07-02 18:50 Asia/Shanghai.

## Current State

- Repository branch: `codex/meso-compression-batch-pipeline`.
- Latest local commits on this branch:
  - `916b9db Add calibration status workbook sheets`
  - `b6ef78b Refresh calibration plan after executed batches`
  - `ec2b12a Accept qualifying reference compression results`
  - `c717379 Add adaptive compression calibration execution`
- These commits may still be local-only on the original machine unless the user explicitly approves pushing to the remote.
- Current calibration output root: `D:\ZDYF_NBY\abqbatch\runs\compression_strength_calibration`.
- Current manifest: `runs\compression_strength_calibration\calibration_manifest.json`.
- Current workbook: `runs\compression_strength_calibration\calibration_summary.xlsx`.
- Current next-candidate CSV: `runs\compression_strength_calibration\next_candidates.csv`.
- Current plan phase: `pre_execution` for `calibration_20260702_batch_07`.
- Current status counts from `calibration_summary.xlsx`: accepted `58`, max-attempts `7`, planned `39`.
- Simulation points: `REFERENCE_SIM=105`, `CURRENT_SIM=37`, total `142`.
- Targets: total `107`, active `104`, excluded `3`.
- Archived calibration ODB count so far: `74`.
- D-drive calibration batch directories currently have no `.odb` residuals after batch 06.
- No Abaqus solver should be running before takeover. Check again before submitting anything.

The previous Codex was told to stop after batch 06. Do not start batch 07 unless the user explicitly authorizes it in the active thread.

## Data Source Contract

The calibration has strict source separation.

- Target strength source: `D:\ZDYF_NBY\静强度数据集.xlsx`, Sheet1, column 5. This is `TARGET_ONLY`.
- Previous-round actual simulation reference: `D:\ZDYF_NBY\abqbatch\runs\puckzt_angle_0p8_g1c5_compression_all\reports\meso_compression_all_batches_summary.xlsx`. This is `REFERENCE_SIM`.
- Current calibration history: `D:\ZDYF_NBY\abqbatch\runs\compression_strength_calibration\calibration_history.csv`. This is `CURRENT_SIM`.
- The target workbook must never be used as an interpolation or trend source.
- Interpolation may only use `REFERENCE_SIM` and `CURRENT_SIM` records.
- Error and acceptance are computed against `TARGET_ONLY` only.
- Acceptance criterion: absolute relative strength error `<=15%`.
- Old `REFERENCE_SIM` points are accepted directly if they already satisfy `<=15%`, even if their angle is outside the new candidate range.

Excluded from continuing calibration:

```text
qj-24-24
13-24-24
13-24-48
13-24-72
```

Special source deck rule:

```text
132-48-72 uses D:\ZDYF-NBY-ZJY\资料\inp\inp统计-JQ\inp统计-JQ\132-48-72-jq.inp
```

## Batch 07 Candidates

The next planned batch is `calibration_20260702_batch_07`.

```text
slot  case_id   attempt  angle_deg  g1c  target_strength_mpa  reason
1     13-60-60  3        2.0        5.0  159.13               nearest_sim_above_target_reduce_strength
2     13-60-72  3        2.0        5.0  140.62               nearest_sim_above_target_reduce_strength
3     13-72-36  3        2.0        5.0  195.35               nearest_sim_above_target_reduce_strength
4     13-72-48  2        2.5        5.0  164.17               nearest_sim_above_target_reduce_strength
5     13-72-60  1        3.0        5.0  153.48               nearest_sim_above_target_reduce_strength
6     13-72-72  1        3.0        5.0  145.53               nearest_sim_above_target_reduce_strength
```

The max-attempt cases already recorded before batch 07 are:

```text
13-36-60 best_error_pct=19.422904
13-36-72 best_error_pct=35.360164
13-48-36 best_error_pct=16.239895
13-48-48 best_error_pct=31.978848
13-48-72 best_error_pct=42.83314
13-60-36 best_error_pct=23.941184
13-60-48 best_error_pct=21.755236
```

## Required External Inputs

```text
base params JSON:
D:\ZDYF_NBY\13-24-24-YS-simulation\07_validation\meso_vs_experiment\compression\puckzt_angle_0p8_g1c5_input_parameters.json

previous reference summary:
D:\ZDYF_NBY\abqbatch\runs\puckzt_angle_0p8_g1c5_compression_all\reports\meso_compression_all_batches_summary.xlsx

target workbook:
D:\ZDYF_NBY\静强度数据集.xlsx

archive root:
E:\ZDYF_NBY_abqbatch_archive

Abaqus wrapper:
C:\Users\11843\codex_abaqus.cmd
```

Never call raw `abaqus` from Codex. Always use the wrapper, directly or through the pipeline.

## Preflight On The Takeover Machine

From `D:\ZDYF_NBY\abqbatch`:

```powershell
git branch --show-current
git log --oneline -8
Get-Process | Where-Object { $_.ProcessName -in @('standard','explicit','pre','abq2021','ABQcaeK','ABQLauncher','SMAJobManager') }
Get-ChildItem -Path runs\compression_strength_calibration\batches -Recurse -Filter *.odb -File -ErrorAction SilentlyContinue | Select-Object -First 20 FullName,Length
& 'C:\Users\11843\codex_abaqus.cmd' information=environment
```

Expected before starting batch 07:

- Branch is `codex/meso-compression-batch-pipeline`.
- Latest commit includes `916b9db Add calibration status workbook sheets`.
- No solver process is running.
- No `.odb` appears under `runs\compression_strength_calibration\batches`.
- Abaqus environment is reachable through `C:\Users\11843\codex_abaqus.cmd`.

## Refresh Plan Without Running Abaqus

Use this safe command to rebuild `calibration_manifest.json`, `next_candidates.csv`, and `calibration_summary.xlsx` from current history. It does not start Abaqus.

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
$script = @"
import sys
from pathlib import Path
sys.path.insert(0, 'src')
from abqbatch.meso_compression import calibrate_strength_command

target = Path('D:/ZDYF_NBY') / '\u9759\u5f3a\u5ea6\u6570\u636e\u96c6.xlsx'
calibrate_strength_command(
    target_xlsx=target,
    reference_summary_xlsx=[Path(r'D:\ZDYF_NBY\abqbatch\runs\puckzt_angle_0p8_g1c5_compression_all\reports\meso_compression_all_batches_summary.xlsx')],
    params_json=Path(r'D:\ZDYF_NBY\13-24-24-YS-simulation\07_validation\meso_vs_experiment\compression\puckzt_angle_0p8_g1c5_input_parameters.json'),
    current_summary_xlsx=None,
    current_history_csv=Path(r'runs\compression_strength_calibration\calibration_history.csv'),
    output_root=Path(r'runs\compression_strength_calibration'),
    case_id=None,
    batch_size=6,
    max_new_attempts=3,
    max_batches=1,
    execute=False,
    dry_run=False,
    force=False,
    batch_id='calibration_20260702_batch_07',
    archive_root=Path(r'E:\ZDYF_NBY_abqbatch_archive'),
    no_archive=False,
    reference_angle_deg=0.8,
    reference_g1c=5.0,
)
"@
$script | python -B -
```

## Start Batch 07 After Explicit User Approval

Only after the user explicitly says to resume real simulation, run this from `D:\ZDYF_NBY\abqbatch`. This starts datacheck and then serial analysis through the pipeline, then archives ODB files to E and writes D/E indexes.

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
$script = @"
import sys
from pathlib import Path
sys.path.insert(0, 'src')
from abqbatch.meso_compression import calibrate_strength_command

target = Path('D:/ZDYF_NBY') / '\u9759\u5f3a\u5ea6\u6570\u636e\u96c6.xlsx'
calibrate_strength_command(
    target_xlsx=target,
    reference_summary_xlsx=[Path(r'D:\ZDYF_NBY\abqbatch\runs\puckzt_angle_0p8_g1c5_compression_all\reports\meso_compression_all_batches_summary.xlsx')],
    params_json=Path(r'D:\ZDYF_NBY\13-24-24-YS-simulation\07_validation\meso_vs_experiment\compression\puckzt_angle_0p8_g1c5_input_parameters.json'),
    current_summary_xlsx=None,
    current_history_csv=Path(r'runs\compression_strength_calibration\calibration_history.csv'),
    output_root=Path(r'runs\compression_strength_calibration'),
    case_id=None,
    batch_size=6,
    max_new_attempts=3,
    max_batches=1,
    execute=True,
    dry_run=False,
    force=True,
    batch_id='calibration_20260702_batch_07',
    archive_root=Path(r'E:\ZDYF_NBY_abqbatch_archive'),
    no_archive=False,
    reference_angle_deg=0.8,
    reference_g1c=5.0,
)
"@
$script | python -B -
```

Expected behavior:

- The command prepares six candidate directories under `runs\compression_strength_calibration\batches\calibration_20260702_batch_07`.
- For each candidate it runs datacheck first, then analysis serially.
- It appends six rows to `calibration_history.csv`.
- It moves all `.odb` files for the batch to `E:\ZDYF_NBY_abqbatch_archive\compression_calibration_<stamp>\calibration_20260702_batch_07`.
- It writes `runs\compression_strength_calibration\archive_indexes\calibration_20260702_batch_07_odb_archive_index.csv` on D and a matching index under the E archive directory.
- It refreshes `calibration_manifest.json`, `next_candidates.csv`, and `calibration_summary.xlsx` after execution so they point at the next batch, not the just-completed batch.

## Post-Batch Verification

Run these checks immediately after each batch.

```powershell
$batch='calibration_20260702_batch_07'
$summary="runs\compression_strength_calibration\${batch}_execution_summary.json"
if (Test-Path $summary) {
  $e=Get-Content -Raw $summary | ConvertFrom-Json
  $e.history_rows | Select-Object case_id,attempt_id,angle_deg,g1c,target_strength_mpa,compressive_strength_mpa,error_pct,accepted,status | Format-Table -AutoSize
  $e.archive
}

Get-ChildItem -Path "runs\compression_strength_calibration\batches\$batch" -Recurse -Filter *.odb -File -ErrorAction SilentlyContinue | Select-Object FullName,Length

$idx="runs\compression_strength_calibration\archive_indexes\${batch}_odb_archive_index.csv"
if (Test-Path $idx) {
  "rows=$((Import-Csv $idx).Count)"
  Import-Csv $idx | Where-Object { -not $_.case_id -or -not $_.attempt_id -or -not $_.angle_deg -or -not $_.g1c -or -not $_.sha256 } | ConvertTo-Json
}

Get-Process | Where-Object { $_.ProcessName -in @('standard','explicit','pre','abq2021','ABQcaeK','ABQLauncher','SMAJobManager') } | Select-Object ProcessName,Id,CPU,StartTime
```

Expected after a healthy batch:

- Six history rows exist in the execution summary.
- D-drive batch directory has no `.odb` residuals.
- D archive index has 12 rows: six datacheck ODB files plus six analysis ODB files.
- Every archive index row has `batch_id`, `case_id`, `attempt_id`, `angle_deg`, `g1c`, `original_path`, `archive_path`, `size_bytes`, `sha256`, and `job_name`.
- No solver process remains running.

## Continue Adaptive Calibration After Batch 07

After a batch finishes, inspect the refreshed candidates:

```powershell
Get-Content runs\compression_strength_calibration\next_candidates.csv
$env:PYTHONDONTWRITEBYTECODE='1'
$script = @"
from pathlib import Path
import sys
sys.path.insert(0, 'src')
from abqbatch.meso_compression import read_xlsx_rows
xlsx = Path(r'runs\compression_strength_calibration\calibration_summary.xlsx')
for sheet in ['overview', 'status_summary', 'next_candidates', 'max_attempts', 'archive_summary']:
    print('---', sheet)
    for row in read_xlsx_rows(xlsx, sheet_name=sheet)[:20]:
        print(row)
"@
$script | python -B -
```

If `next_candidates.csv` contains another six candidates and the user has not requested a pause, run the next batch with a new unique `batch_id`, for example `calibration_20260702_batch_08`. Do not reuse an existing batch id unless deliberately rerunning with `force=True` and the user has confirmed overwriting the prepared directory is acceptable.

## Reporting Artifacts

The main workbook is:

```text
D:\ZDYF_NBY\abqbatch\runs\compression_strength_calibration\calibration_summary.xlsx
```

Important sheets:

- `overview`: high-level counts and archive totals.
- `status_summary`: decision status and data-role counts.
- `next_candidates`: next batch to execute.
- `accepted_cases`: cases accepted from old or current actual simulations.
- `max_attempts`: cases that reached three new attempts without satisfying the 15% criterion.
- `archive_summary`: one row per archived calibration batch, with E-drive batch archive directories.
- `archive_index`: row-level ODB archive traceability.
- `decisions`: all active-case decisions.
- `targets`: parsed `TARGET_ONLY` records.
- `simulation_points`: parsed `REFERENCE_SIM` and `CURRENT_SIM` records.

## Git And Remote Notes

The code changes needed for adaptive calibration are committed locally. Do not push to the remote unless the user explicitly authorizes remote push.

If the user approves pushing:

```powershell
git push origin codex/meso-compression-batch-pipeline
```

There are unrelated dirty worktree files on the original machine. Do not revert or include them unless the user explicitly requests it. When committing future fixes, stage only the files changed for the task.

## Hard Guardrails

- Do not use the target workbook as a simulation/reference point source.
- Do not start batch 07 or later without explicit user approval if the last user instruction was to pause.
- Do not call raw `abaqus`; use `C:\Users\11843\codex_abaqus.cmd` through the pipeline.
- Do not leave `.odb` files on D after a completed batch.
- Do not delete `.dat`, curve CSV, summary JSON, state JSON, manifest JSON, history CSV, archive indexes, or workbooks from D.
- Do not mark the goal complete until every active case is accepted or has a documented max-attempt outcome, all ODB files are archived and indexed, and the summary workbook is current.
