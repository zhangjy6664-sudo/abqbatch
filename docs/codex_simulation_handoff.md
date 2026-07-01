# Codex Simulation Handoff

This document is for the Codex instance on another Windows workstation that will take over or audit the Abaqus meso compression and shear simulations.

Last updated: 2026-07-01, Asia/Shanghai.

## Current Repository State

- Repository: `https://github.com/zhangjy6664-sudo/abqbatch.git`
- Working branch to continue from: `codex/meso-compression-batch-pipeline`
- Latest relevant commits:
  - `Add meso shear batch workflow`
  - `9735c6b Add original meso inp decks`
  - `3df3ca0 Add meso compression batch pipeline`
- The raw source decks are committed through Git LFS:
  - `13-inp/`: 35 raw `.inp`
  - `132/`: 36 raw `.inp`
  - `qj/`: 36 raw `.inp`
- Do not use or commit `runs/` as source input. It is run state and reports only.

## Clone On The New Machine

Run this from the target parent directory:

```powershell
git lfs install
git clone --branch codex/meso-compression-batch-pipeline https://github.com/zhangjy6664-sudo/abqbatch.git D:\ZDYF_NBY\abqbatch
Set-Location D:\ZDYF_NBY\abqbatch
git lfs pull
python -m pip install -e ".[dev]"
```

Verify that LFS fetched the real decks, not pointer-only files:

```powershell
Get-ChildItem 13-inp,132,qj -Filter *.inp | Measure-Object Length -Sum
git lfs ls-files | Measure-Object
```

Expected deck count: `107`.

## Required External Inputs

The parameter JSON and UMAT source are outside this repository. Copy them to the new machine or change the `--params-json` path and JSON UMAT paths consistently before preparing new runs.

Required parameter JSON:

```text
D:\ZDYF_NBY\13-24-24-YS-simulation\07_validation\meso_vs_experiment\compression\puckzt_angle_0p8_g1c5_input_parameters.json
```

Expected hashes:

```text
params_sha256=62a5fa3020e209b202343afbee858ccb634f816760b38b1ea1adc752aabc107c
params_payload_sha256=f97f6a7efde9de96b72922ec3533cca19cb83de8f0ad4dfbb612cc8437bf0c3b
umat_source_sha256=627ba062037a7a356fdd11ef66555c90b9fb39e48bd4c9d73e3e1f6ebd702f5b
```

UMAT path recorded in the completed manifests:

```text
D:\ZDYF_NBY\13-24-24-YS-simulation\05_meso_simulation\jobs\compression\umat_candidates\puckzt_angle_0p8_g1c5\puck_zt_current_matrix_candidate_001.for
```

## Abaqus Rules

- Always call `C:\Users\11843\codex_abaqus.cmd`, never raw `abaqus`.
- Run datacheck before analysis.
- Run analyses serially.
- Use `cpus=16` unless the user explicitly changes it.
- Keep raw decks read-only. The pipeline copies and patches into `runs/.../work/cases/.../input/working.inp`.
- Stop no-longer-needed jobs with:

```powershell
& 'C:\Users\11843\codex_abaqus.cmd' terminate job=<job_name>
```

Before starting a new production run, verify:

```powershell
Get-Process | Where-Object { $_.ProcessName -match 'standard|explicit|SMA|abaqus|python' }
Get-ChildItem -Path runs -Recurse -Filter *.lck -File -ErrorAction SilentlyContinue
```

Only `ABAQUSLM` is expected to remain active when no job is running.

## Meso Compression Pipeline Commands

The command group is `abqbatch meso-compression`.

Prepare a new run directory from a raw `.inp` directory:

```powershell
abqbatch meso-compression prepare `
  --params-json "D:\ZDYF_NBY\13-24-24-YS-simulation\07_validation\meso_vs_experiment\compression\puckzt_angle_0p8_g1c5_input_parameters.json" `
  --input-dir "D:\ZDYF_NBY\abqbatch\<raw_inp_dir>" `
  --run-dir "D:\ZDYF_NBY\abqbatch\runs\<new_run_name>"
```

Run datacheck for all prepared cases:

```powershell
abqbatch meso-compression datacheck --run-dir "D:\ZDYF_NBY\abqbatch\runs\<new_run_name>"
```

After datacheck completes, start serial analysis:

```powershell
abqbatch meso-compression run --run-dir "D:\ZDYF_NBY\abqbatch\runs\<new_run_name>"
```

Generate the summary workbook:

```powershell
abqbatch meso-compression report-xlsx --run-dir "D:\ZDYF_NBY\abqbatch\runs\<new_run_name>"
```

Check status at any time:

```powershell
abqbatch meso-compression status --run-dir "D:\ZDYF_NBY\abqbatch\runs\<new_run_name>"
```

## What The Pipeline Patches

For each raw deck, the pipeline preserves geometry, mesh, partitions, sections, equations, and orientation data. It patches only:

- `MATRIX`, `WARP`, and `WEFT` material cards from the JSON.
- `Step-1` increment/static controls.
- Driver boundary displacement.
- DAT node print output for `"Constraints Driver Fx"` with `U` and `RF`.
- UMAT working copy into the case input directory.

Material contract expected by the current UMAT:

- `MATRIX`: `*Depvar` 11, `*User Material, constants=12`.
- `WARP` / `WEFT`: `*Depvar` 80, `*User Material, constants=17`.
- WARP/WEFT constant order:

```text
E11,E22,G12,G23,NU12,NU23,S1T,S1C,S2T,S2C,S12,S23,G1T,G1C,G2T,G2C,ETA
```

Current run settings from the manifests:

```text
candidate_id=puckzt_angle_0p8_g1c5
abaqus_cmd=C:\Users\11843\codex_abaqus.cmd
cpus=16
drop_fraction=0.02
poll_seconds=15
min_points=10
min_peak_mpa=100
modulus_window_start=0.0021
modulus_window_end=0.0052
```

## Completed Batches On The Original Machine

These batches are already complete on the original workstation:

```text
puckzt_angle_0p8_g1c5_compression_35inp: SOLVED=35
puckzt_angle_0p8_g1c5_compression_132:   SOLVED=31, ANALYSIS_FAILED=4, DATACHECK_FAILED=1
puckzt_angle_0p8_g1c5_compression_qj:    SOLVED=30, ANALYSIS_FAILED=6
```

Known non-solved cases:

```text
132-48-24: ANALYSIS_FAILED, no valid metrics extracted.
132-48-72: DATACHECK_FAILED. Raw deck structure issue: misplaced *ENDASSEMBLY around working.inp line 192014.
132-60-12: ANALYSIS_FAILED, metrics extracted.
132-72-12: ANALYSIS_FAILED, metrics extracted.
132-72-24: ANALYSIS_FAILED, metrics extracted.
QJ-48-72: ANALYSIS_FAILED, metrics extracted.
QJ-60-48: ANALYSIS_FAILED, metrics extracted.
QJ-60-60: ANALYSIS_FAILED, metrics extracted.
QJ-60-72: ANALYSIS_FAILED, metrics extracted.
QJ-72-12: ANALYSIS_FAILED, metrics extracted.
QJ-72-48: ANALYSIS_FAILED, metrics extracted.
```

The failure states are intentional records; they should not block new batches.

## Local Reports And Archive On The Original Machine

Run summaries on the original workstation:

```text
D:\ZDYF_NBY\abqbatch\runs\puckzt_angle_0p8_g1c5_compression_35inp\reports\meso_compression_summary.xlsx
D:\ZDYF_NBY\abqbatch\runs\puckzt_angle_0p8_g1c5_compression_132\reports\meso_compression_summary.xlsx
D:\ZDYF_NBY\abqbatch\runs\puckzt_angle_0p8_g1c5_compression_qj\reports\meso_compression_summary.xlsx
D:\ZDYF_NBY\abqbatch\runs\puckzt_angle_0p8_g1c5_compression_all\reports\meso_compression_all_batches_summary.xlsx
```

Large historical run files were moved off D: to E: on the original workstation:

```text
E:\ZDYF_NBY_abqbatch_archive\runs_archive_20260629_142204
D:\ZDYF_NBY\abqbatch\runs\archive_indexes\archive_index_20260629_142204.csv
```

Moved file classes:

```text
.dat
.inp
.msg
```

Count moved: `746`. Size moved: about `12.541 GB`.

If a previous run's `working.inp`, raw copy, DAT, or MSG is needed for audit, restore it by searching the archive index by `original_path`, `run`, or `case_id`. The D: `runs` directory intentionally no longer contains those large historical files.

## Completed Meso Shear Batch On The Original Machine

Selected shear parameter JSON:

```text
D:\ZDYF_NBY\13-24-24-JQ-simulation\07_validation\final_selected_parameters\13_24_24_jq_shear_selected_hashin_vf053_beta2p2_pa1_0.json
```

Pipeline command group: `abqbatch meso-shear`.

Final status after the 2026-07-01 repair run:

```text
13-inp / jq_shear_hashin_vf053_beta2p2_pa1_0_35inp: SOLVED=35
132    / jq_shear_hashin_vf053_beta2p2_pa1_0_132:   SOLVED=36
qj     / jq_shear_hashin_vf053_beta2p2_pa1_0_qj:    SOLVED=36
all:                                                SOLVED=107
```

`132-48-72` was repaired after the initial datacheck failure. The broken 132 raw deck was missing the closing part/assembly structure, so that case now uses the complete JQ deck copy at `D:\ZDYF_NBY\abqbatch\runs\jq_shear_hashin_vf053_beta2p2_pa1_0_132\repair_sources\132-48-72-jq.inp`; the generated `working.inp` patches `"Constraints Driver Shear_yx", 1, 1, 0.0800000000`. It passed datacheck and solved. Final metrics: 123 curve points, shear modulus 1.9558455805912422 GPa, stress at 0.05 strain 70.52457266858343 MPa, peak within 0.05 strain 70.20779992495136 MPa at strain 0.0496.

Shear report workbooks on D:

```text
D:\ZDYF_NBY\abqbatch\runs\jq_shear_hashin_vf053_beta2p2_pa1_0_35inp\reports\meso_shear_summary.xlsx
D:\ZDYF_NBY\abqbatch\runs\jq_shear_hashin_vf053_beta2p2_pa1_0_132\reports\meso_shear_summary.xlsx
D:\ZDYF_NBY\abqbatch\runs\jq_shear_hashin_vf053_beta2p2_pa1_0_qj\reports\meso_shear_summary.xlsx
D:\ZDYF_NBY\abqbatch\runs\jq_shear_hashin_vf053_beta2p2_pa1_0_all\reports\meso_shear_summary_all.xlsx
```

Shear archive indexes on D:

```text
D:\ZDYF_NBY\abqbatch\runs\jq_shear_hashin_vf053_beta2p2_pa1_0_35inp\reports\archive_index.csv
D:\ZDYF_NBY\abqbatch\runs\jq_shear_hashin_vf053_beta2p2_pa1_0_132\reports\archive_index.csv
D:\ZDYF_NBY\abqbatch\runs\jq_shear_hashin_vf053_beta2p2_pa1_0_qj\reports\archive_index.csv
D:\ZDYF_NBY\abqbatch\runs\jq_shear_hashin_vf053_beta2p2_pa1_0_all\reports\archive_index_all.csv
```

Archived shear output root on E:

```text
E:\ZDYF_NBY_abqbatch_archive\jq_shear_hashin_vf053_beta2p2_pa1_0
```

Archive index row counts:

```text
35inp: 525
132:   533
qj:    540
all:   1598
```

The D: shear work directories retain `manifest.json`, `state.json`, validation files, command logs, summary JSON, curve CSV, original inp copies, and working inp copies. Top-level Abaqus output classes such as `.odb`, `.stt`, `.sim`, `.mdl`, `.prt`, `.dat`, `.msg`, `.com`, `.023`, `.sta`, and `.log` were copied to E:, SHA256-verified, and removed from D:.

To prepare a new shear batch on another machine, use the same selected JSON and raw deck directory:

```powershell
abqbatch meso-shear prepare `
  --selected-json "D:\ZDYF_NBY\13-24-24-JQ-simulation\07_validation\final_selected_parameters\13_24_24_jq_shear_selected_hashin_vf053_beta2p2_pa1_0.json" `
  --input-dir "D:\ZDYF_NBY\abqbatch\qj" `
  --run-dir "D:\ZDYF_NBY\abqbatch\runs\jq_shear_hashin_vf053_beta2p2_pa1_0_qj_retry_01"

abqbatch meso-shear smoke --run-dir "D:\ZDYF_NBY\abqbatch\runs\jq_shear_hashin_vf053_beta2p2_pa1_0_qj_retry_01" --case QJ-12-12
abqbatch meso-shear datacheck --run-dir "D:\ZDYF_NBY\abqbatch\runs\jq_shear_hashin_vf053_beta2p2_pa1_0_qj_retry_01"
abqbatch meso-shear run --run-dir "D:\ZDYF_NBY\abqbatch\runs\jq_shear_hashin_vf053_beta2p2_pa1_0_qj_retry_01"
abqbatch meso-shear report-xlsx --run-dir "D:\ZDYF_NBY\abqbatch\runs\jq_shear_hashin_vf053_beta2p2_pa1_0_qj_retry_01"
```

## Recommended Recovery Strategy For Old Failures

Do not rerun old failed cases directly inside the archived historical run directories unless the needed `input/*.inp` files have been restored from E:.

Preferred approach for retrying old failures:

1. Create a new retry run directory from the committed raw source directory.
2. Use the same parameter JSON and UMAT.
3. Run datacheck first.
4. Run analysis serially.
5. Generate a new workbook and compare against the previous summary.

Example for retrying all `qj` decks into a new run directory:

```powershell
abqbatch meso-compression prepare `
  --params-json "D:\ZDYF_NBY\13-24-24-YS-simulation\07_validation\meso_vs_experiment\compression\puckzt_angle_0p8_g1c5_input_parameters.json" `
  --input-dir "D:\ZDYF_NBY\abqbatch\qj" `
  --run-dir "D:\ZDYF_NBY\abqbatch\runs\puckzt_angle_0p8_g1c5_compression_qj_retry_01"

abqbatch meso-compression datacheck --run-dir "D:\ZDYF_NBY\abqbatch\runs\puckzt_angle_0p8_g1c5_compression_qj_retry_01"
abqbatch meso-compression run --run-dir "D:\ZDYF_NBY\abqbatch\runs\puckzt_angle_0p8_g1c5_compression_qj_retry_01"
abqbatch meso-compression report-xlsx --run-dir "D:\ZDYF_NBY\abqbatch\runs\puckzt_angle_0p8_g1c5_compression_qj_retry_01"
```

For a single-case retry, use the Python API so only that case is submitted:

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
@'
from pathlib import Path
from abqbatch.meso_compression import datacheck_cases, analysis_cases, collect_status, write_results_xlsx

run_dir = Path(r"D:\ZDYF_NBY\abqbatch\runs\<retry_run_dir>")
case_ids = ["QJ-48-72"]
datacheck_cases(run_dir, case_ids=case_ids)
analysis_cases(run_dir, case_ids=case_ids)
print(collect_status(run_dir)["counts"])
print(write_results_xlsx(run_dir))
'@ | python -
```

## Disk Hygiene During Long Runs

Do not delete large Abaqus outputs as a cleanup strategy. The current rule is:

1. Extract DAT curves and write `summary.json` / curve CSV first.
2. Copy heavy outputs to E: under the configured archive root.
3. Verify SHA256 for the copied file.
4. Remove the D: source only after verification succeeds.
5. Write `archive_index.csv` and `archive_index.json` in the run's `reports/` directory.

The shear pipeline does this automatically after each analysis case and after a failed datacheck case. If a rerun produces a different file with an already archived name, the existing E: file is preserved and the new file is archived with a timestamp/hash suffix. Manual backfill uses the same verified archive path:

```powershell
abqbatch meso-shear archive-heavy --run-dir "D:\ZDYF_NBY\abqbatch\runs\<run_dir>"
```

Do not remove `state.json`, `manifest.json`, `validation.json`, `summary.json`, curve CSVs, original/working inp copies under `input/`, command logs, archive indexes, or report workbooks unless the run is intentionally discarded.

## Minimum Handoff Checklist

Before the new Codex submits any Abaqus job:

- `git branch --show-current` returns `codex/meso-compression-batch-pipeline`.
- `git lfs ls-files` reports `107` tracked `.inp` decks.
- Parameter JSON and UMAT source exist on the new machine.
- `C:\Users\11843\codex_abaqus.cmd information=environment` works and shows the safe wrapper environment.
- No stale `.lck` files exist in the target run directory.
- No `standard.exe` or `explicit.exe` process is already running.
- The next run directory name is new and does not overwrite a completed run.
