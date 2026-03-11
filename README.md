# ttrack

`ttrack.py` is a Windows activity tracker that writes daily JSONL/CSV logs and a local HTML report under `out/YYYY-MM-DD/`.

## HTML report

The generated report includes:

- daily active and idle totals
- a timeline view grouped by process
- per-process and per-window summaries
- a recent segments table

For readability, report timestamps in the recent segments table and timeline hover text are displayed as `YYYY-MM-DD HH:MM:SS`. The underlying JSONL and CSV logs keep their original ISO 8601 timestamps with timezone offsets.
