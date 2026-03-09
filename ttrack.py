#!/usr/bin/env python3
"""
Windows Time Tracker (single-file, stdlib only)

What it does
------------
- Detects system-wide idle time using the Windows API
- Tracks foreground window title while active
- Tries to capture process name too
- Writes durable daily JSONL and CSV logs
- Generates a local daily HTML report with:
  - daily totals
  - a ManicTime-like timeline view
  - per-process totals
  - per-window totals
  - recent activity segments

No third-party packages required.

Usage
-----
python time_tracker_windows.py
python time_tracker_windows.py --threshold-minutes 5 --poll-seconds 1
python time_tracker_windows.py --output-dir ".\\out"

Notes
-----
- This tracks activity/idle state, not the actual keys you type.
- It is designed to be friendlier to locked-down machines than hook-based tools.
- Run it in a normal user session on Windows.
- Output rotates daily into out/YYYY-MM-DD/ to keep files smaller and reporting snappier.
"""

from __future__ import annotations

import argparse
import csv
import ctypes
from ctypes import wintypes
import datetime as dt
import html
import json
import os
from pathlib import Path
import sys
import time
from dataclasses import dataclass, asdict
from typing import Optional, Iterable


# =========================
# Windows API setup
# =========================

if os.name != "nt":
    raise SystemExit("This script only runs on Windows.")

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("dwTime", wintypes.DWORD),
    ]


user32.GetLastInputInfo.argtypes = [ctypes.POINTER(LASTINPUTINFO)]
user32.GetLastInputInfo.restype = wintypes.BOOL

user32.GetForegroundWindow.argtypes = []
user32.GetForegroundWindow.restype = wintypes.HWND

user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int

user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int

user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD

kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE

kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL

kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL

kernel32.GetTickCount64.argtypes = []
kernel32.GetTickCount64.restype = ctypes.c_ulonglong

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


# =========================
# Data model
# =========================

@dataclass
class Segment:
    kind: str                   # "active" or "idle"
    start_iso: str
    end_iso: Optional[str]
    duration_seconds: Optional[int]
    window_title: str
    process_name: str
    pid: Optional[int]


# =========================
# Utility functions
# =========================

def now_local() -> dt.datetime:
    return dt.datetime.now().astimezone()

def iso_zoned(ts: dt.datetime) -> str:
    return ts.isoformat(timespec="seconds")

def parse_iso(ts: str) -> dt.datetime:
    return dt.datetime.fromisoformat(ts)

def fmt_hms(seconds: int) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"

def fmt_short(ts: dt.datetime) -> str:
    return ts.strftime("%Y-%m-%d %H:%M:%S")

def date_str(ts: dt.datetime) -> str:
    return ts.strftime("%Y-%m-%d")

def sanitize_filename(name: str) -> str:
    bad = '<>:"/\\|?*'
    out = "".join("_" if c in bad else c for c in name)
    return out.strip() or "time_tracker"

def get_idle_seconds() -> int:
    lii = LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not user32.GetLastInputInfo(ctypes.byref(lii)):
        raise ctypes.WinError()

    now_ticks = kernel32.GetTickCount64()
    last_input_ticks = lii.dwTime
    elapsed_ms = (now_ticks - last_input_ticks) & 0xFFFFFFFF
    return int(elapsed_ms // 1000)

def get_foreground_hwnd():
    return user32.GetForegroundWindow()

def get_window_title(hwnd) -> str:
    if not hwnd:
        return ""
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return (buf.value or "").strip()

def get_window_pid(hwnd) -> Optional[int]:
    if not hwnd:
        return None
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value) if pid.value else None

def get_process_path(pid: Optional[int]) -> str:
    if not pid:
        return ""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(2048)
        buf = ctypes.create_unicode_buffer(size.value)
        ok = kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size))
        return buf.value if ok else ""
    finally:
        kernel32.CloseHandle(handle)

def get_process_name(pid: Optional[int]) -> str:
    path = get_process_path(pid)
    return os.path.basename(path) if path else ""

def classify_state(idle_seconds: int, idle_threshold_seconds: int) -> str:
    return "idle" if idle_seconds >= idle_threshold_seconds else "active"

def html_escape(s: str) -> str:
    return html.escape(s or "", quote=True)


# =========================
# Tracker
# =========================

class Tracker:
    def __init__(
        self,
        output_dir: Path,
        idle_threshold_seconds: int,
        poll_seconds: float,
        report_every_seconds: float,
    ) -> None:
        self.output_dir = output_dir
        self.idle_threshold_seconds = int(idle_threshold_seconds)
        self.poll_seconds = float(poll_seconds)
        self.report_every_seconds = max(10.0, float(report_every_seconds))

        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.current_segment: Optional[Segment] = None
        self.started_at = now_local()
        self.last_report_build_ts = 0.0

    def current_day_str(self) -> str:
        return date_str(now_local())

    def day_paths(self, day: str) -> tuple[Path, Path, Path]:
        day_dir = self.output_dir / day
        day_dir.mkdir(parents=True, exist_ok=True)
        return (
            day_dir / "segments.jsonl",
            day_dir / "segments.csv",
            day_dir / "report.html",
        )

    def current_paths(self) -> tuple[Path, Path, Path]:
        return self.day_paths(self.current_day_str())


    def sample_context(self):
        hwnd = get_foreground_hwnd()
        pid = get_window_pid(hwnd)
        title = get_window_title(hwnd)
        process_name = get_process_name(pid)
        idle_seconds = get_idle_seconds()
        state = classify_state(idle_seconds, self.idle_threshold_seconds)
        return state, title, process_name, pid, idle_seconds

    def segment_key(self, kind: str, title: str, process_name: str, pid: Optional[int]):
        # Idle is treated as one logical bucket, rather than many window-specific idle states.
        if kind == "idle":
            return ("idle", "", "", None)
        return (kind, title, process_name, pid)

    def open_segment(self, kind: str, title: str, process_name: str, pid: Optional[int], start_ts: dt.datetime):
        self.current_segment = Segment(
            kind=kind,
            start_iso=iso_zoned(start_ts),
            end_iso=None,
            duration_seconds=None,
            window_title=title,
            process_name=process_name,
            pid=pid,
        )

    def close_segment(self, end_ts: dt.datetime) -> None:
        if not self.current_segment:
            return
        start_ts = parse_iso(self.current_segment.start_iso)
        dur = max(0, int((end_ts - start_ts).total_seconds()))
        self.current_segment.end_iso = iso_zoned(end_ts)
        self.current_segment.duration_seconds = dur
        segments_jsonl, _, _ = self.day_paths(date_str(start_ts))
        with segments_jsonl.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(self.current_segment), ensure_ascii=False) + "\n")
        self.current_segment = None

    def step(self) -> None:
        ts = now_local()
        kind, title, process_name, pid, idle_seconds = self.sample_context()

        new_key = self.segment_key(kind, title, process_name, pid)
        if self.current_segment is None:
            self.open_segment(kind, title, process_name, pid, ts)
            return

        cur_key = self.segment_key(
            self.current_segment.kind,
            self.current_segment.window_title,
            self.current_segment.process_name,
            self.current_segment.pid,
        )

        if new_key != cur_key:
            self.close_segment(ts)
            self.open_segment(kind, title, process_name, pid, ts)

    def load_segments(self, day: Optional[str] = None) -> list[dict]:
        rows: list[dict] = []
        target_day = day or self.current_day_str()
        segments_jsonl, _, _ = self.day_paths(target_day)
        if not segments_jsonl.exists():
            return rows
        with segments_jsonl.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return rows

    def write_csv(self, rows: Iterable[dict], day: Optional[str] = None) -> None:
        target_day = day or self.current_day_str()
        _, segments_csv, _ = self.day_paths(target_day)
        with segments_csv.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=[
                    "kind",
                    "start_iso",
                    "end_iso",
                    "duration_seconds",
                    "window_title",
                    "process_name",
                    "pid",
                ],
            )
            writer.writeheader()
            for row in rows:
                writer.writerow(row)

    def build_report(self, rows: list[dict], day: Optional[str] = None) -> None:
        daily = {}
        process_totals = {}
        window_totals = {}

        for row in rows:
            kind = row.get("kind", "")
            dur = int(row.get("duration_seconds") or 0)
            start_iso = row.get("start_iso")
            try:
                start_ts = parse_iso(start_iso)
                day_key = date_str(start_ts)
            except Exception:
                day_key = "unknown"

            daily.setdefault(day_key, {"active": 0, "idle": 0})
            if kind == "active":
                daily[day_key]["active"] += dur
            elif kind == "idle":
                daily[day_key]["idle"] += dur

            if kind == "active":
                proc = row.get("process_name") or "(unknown)"
                process_totals[proc] = process_totals.get(proc, 0) + dur

                title = row.get("window_title") or "(untitled window)"
                window_key = f"{proc} :: {title}"
                window_totals[window_key] = window_totals.get(window_key, 0) + dur

        daily_rows = []
        for day_key, vals in sorted(daily.items(), reverse=True):
            daily_rows.append(
                f"<tr><td>{html_escape(day_key)}</td>"
                f"<td class='mono'>{fmt_hms(vals['active'])}</td>"
                f"<td class='mono'>{fmt_hms(vals['idle'])}</td></tr>"
            )

        proc_rows = []
        for proc, dur in sorted(process_totals.items(), key=lambda kv: kv[1], reverse=True)[:200]:
            proc_rows.append(
                f"<tr><td>{html_escape(proc)}</td><td class='mono'>{fmt_hms(dur)}</td></tr>"
            )

        window_rows = []
        for title, dur in sorted(window_totals.items(), key=lambda kv: kv[1], reverse=True)[:300]:
            window_rows.append(
                f"<tr><td>{html_escape(title)}</td><td class='mono'>{fmt_hms(dur)}</td></tr>"
            )

        recent_rows = []
        for row in sorted(rows, key=lambda r: r.get("start_iso", ""), reverse=True)[:500]:
            recent_rows.append(
                "<tr>"
                f"<td>{html_escape(row.get('kind', ''))}</td>"
                f"<td>{html_escape(row.get('process_name') or '')}</td>"
                f"<td>{html_escape(row.get('window_title') or '')}</td>"
                f"<td class='mono'>{html_escape(row.get('start_iso') or '')}</td>"
                f"<td class='mono'>{html_escape(row.get('end_iso') or '')}</td>"
                f"<td class='mono'>{fmt_hms(int(row.get('duration_seconds') or 0))}</td>"
                "</tr>"
            )

        total_active = sum(int(r.get("duration_seconds") or 0) for r in rows if r.get("kind") == "active")
        total_idle = sum(int(r.get("duration_seconds") or 0) for r in rows if r.get("kind") == "idle")

        # Build a simple ManicTime-like timeline for the selected day.
        target_day = day or self.current_day_str()
        timeline_day_rows = []
        min_minutes = 24 * 60
        lane_height = 18
        lane_gap = 8
        block_height = 14

        process_palette = [
            "#60a5fa", "#34d399", "#f472b6", "#f59e0b", "#a78bfa",
            "#22d3ee", "#fb7185", "#4ade80", "#c084fc", "#f97316",
            "#2dd4bf", "#818cf8", "#e879f9", "#84cc16", "#38bdf8",
        ]
        lane_map = {}
        lane_names = []
        color_map = {}
        color_idx = 0

        def minute_of_day(ts: dt.datetime) -> int:
            return ts.hour * 60 + ts.minute

        # Determine lanes from active process names, idle as its own lane.
        for row in rows:
            try:
                start_ts = parse_iso(row.get("start_iso"))
            except Exception:
                continue
            if date_str(start_ts) != target_day:
                continue
            kind = row.get("kind", "")
            proc = row.get("process_name") or "(unknown)"
            lane_name = "Idle" if kind == "idle" else proc
            if lane_name not in lane_map:
                lane_map[lane_name] = len(lane_names)
                lane_names.append(lane_name)
                if lane_name == "Idle":
                    color_map[lane_name] = "#f59e0b"
                else:
                    color_map[lane_name] = process_palette[color_idx % len(process_palette)]
                    color_idx += 1

        # Sort to keep Idle at bottom and processes alphabetically.
        ordered_names = sorted([n for n in lane_names if n != "Idle"], key=str.lower)
        if "Idle" in lane_map:
            ordered_names.append("Idle")
        lane_map = {name: i for i, name in enumerate(ordered_names)}
        lane_names = ordered_names

        total_height = max(60, len(lane_names) * (lane_height + lane_gap) + 20)
        svg_width = 1440
        label_width = 180
        chart_width = 1200
        chart_x0 = label_width
        minute_px = chart_width / min_minutes

        hour_lines = []
        for h in range(25):
            x = chart_x0 + (h * 60 * minute_px)
            label = f"{h:02d}:00" if h < 24 else ""
            hour_lines.append(
                f"<line x1='{x:.2f}' y1='0' x2='{x:.2f}' y2='{total_height}' stroke='#31405f' stroke-width='1' />"
            )
            if h < 24:
                hour_lines.append(
                    f"<text x='{x + 3:.2f}' y='14' fill='#aab7d8' font-size='11'>{label}</text>"
                )

        lane_guides = []
        lane_labels = []
        blocks = []
        tooltips = []

        for idx, name in enumerate(lane_names):
            y = 24 + idx * (lane_height + lane_gap)
            lane_guides.append(
                f"<line x1='{chart_x0}' y1='{y + lane_height:.2f}' x2='{chart_x0 + chart_width}' y2='{y + lane_height:.2f}' stroke='#22304d' stroke-width='1' />"
            )
            lane_labels.append(
                f"<text x='8' y='{y + 12:.2f}' fill='#e8eefc' font-size='12'>{html_escape(name)}</text>"
            )

        for i, row in enumerate(rows):
            try:
                start_ts = parse_iso(row.get("start_iso"))
                end_ts = parse_iso(row.get("end_iso"))
            except Exception:
                continue
            if date_str(start_ts) != target_day:
                continue

            kind = row.get("kind", "")
            proc = row.get("process_name") or "(unknown)"
            lane_name = "Idle" if kind == "idle" else proc
            if lane_name not in lane_map:
                continue
            lane = lane_map[lane_name]
            y = 24 + lane * (lane_height + lane_gap) + 2

            start_min = minute_of_day(start_ts) + (start_ts.second / 60.0)
            end_min = minute_of_day(end_ts) + (end_ts.second / 60.0)
            x = chart_x0 + start_min * minute_px
            w = max(1.2, (end_min - start_min) * minute_px)

            title = row.get("window_title") or lane_name
            color = color_map.get(lane_name, "#60a5fa")
            opacity = "0.9" if kind == "active" else "0.75"
            tooltip = (
                f"{kind.title()} | {proc or '(unknown)'} | {title} | "
                f"{row.get('start_iso')} → {row.get('end_iso')} | "
                f"{fmt_hms(int(row.get('duration_seconds') or 0))}"
            )
            block_id = f"seg{i}"
            blocks.append(
                f"<rect id='{block_id}' x='{x:.2f}' y='{y:.2f}' width='{w:.2f}' height='{block_height}' "
                f"rx='3' ry='3' fill='{color}' fill-opacity='{opacity}' stroke='#0b1020' stroke-width='0.5'>"
                f"<title>{html_escape(tooltip)}</title></rect>"
            )

        if lane_names:
            timeline_svg = f"""
            <div class="timeline-wrap">
              <svg viewBox="0 0 {svg_width} {total_height}" width="100%" height="{total_height}" role="img" aria-label="Timeline of activity by process">
                <rect x="0" y="0" width="{svg_width}" height="{total_height}" fill="#10192e" />
                {''.join(hour_lines)}
                {''.join(lane_guides)}
                {''.join(lane_labels)}
                {''.join(blocks)}
              </svg>
            </div>
            """
        else:
            timeline_svg = "<div class='small'>No timeline data yet for this day.</div>"

        legend_items = []
        for name in lane_names[:20]:
            legend_items.append(
                f"<span class='legend-item'><span class='legend-swatch' style='background:{color_map.get(name, '#60a5fa')}'></span>{html_escape(name)}</span>"
            )
        legend_html = "".join(legend_items) or "<span class='small'>No legend data yet.</span>"

        report = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Windows Time Tracker Report</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root {{
  --bg: #0b1020;
  --panel: #121a2e;
  --panel2: #1a2440;
  --text: #e8eefc;
  --muted: #aab7d8;
  --line: #31405f;
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0;
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  background: linear-gradient(180deg, #08101d 0%, #0f1730 100%);
  color: var(--text);
}}
.wrap {{
  max-width: 1500px;
  margin: 0 auto;
  padding: 24px;
}}
.card {{
  background: rgba(18, 26, 46, 0.95);
  border: 1px solid var(--line);
  border-radius: 18px;
  padding: 18px;
  margin-bottom: 18px;
  box-shadow: 0 12px 30px rgba(0,0,0,0.22);
}}
.grid {{
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 16px;
}}
h1, h2 {{
  margin-top: 0;
}}
.small {{
  color: var(--muted);
}}
.metric {{
  font-size: 1.9rem;
  font-weight: 700;
  margin-top: 6px;
}}
.mono {{
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}}
table {{
  width: 100%;
  border-collapse: collapse;
}}
th, td {{
  border-bottom: 1px solid var(--line);
  padding: 8px 10px;
  text-align: left;
  vertical-align: top;
}}
th {{
  color: var(--muted);
  position: sticky;
  top: 0;
  background: var(--panel);
}}
.scroll {{
  max-height: 520px;
  overflow: auto;
  border: 1px solid var(--line);
  border-radius: 12px;
}}
.timeline-wrap {{
  overflow-x: auto;
  border: 1px solid var(--line);
  border-radius: 12px;
  background: #10192e;
}}
.legend {{
  display: flex;
  flex-wrap: wrap;
  gap: 10px 16px;
  margin-top: 12px;
}}
.legend-item {{
  display: inline-flex;
  align-items: center;
  gap: 8px;
  color: var(--muted);
  font-size: 0.92rem;
}}
.legend-swatch {{
  width: 12px;
  height: 12px;
  border-radius: 3px;
  display: inline-block;
}}
@media (max-width: 1100px) {{
  .grid {{ grid-template-columns: 1fr; }}
}}
</style>
</head>
<body>
<div class="wrap">
  <div class="card">
    <h1>Windows Time Tracker Report</h1>
    <div class="small">
      Generated {html_escape(fmt_short(now_local()))} · Output folder: {html_escape(str(self.output_dir / target_day))}
    </div>
  </div>

  <div class="grid">
    <div class="card">
      <div class="small">Total active</div>
      <div class="metric mono">{fmt_hms(total_active)}</div>
    </div>
    <div class="card">
      <div class="small">Total idle</div>
      <div class="metric mono">{fmt_hms(total_idle)}</div>
    </div>
    <div class="card">
      <div class="small">Segments recorded</div>
      <div class="metric mono">{len(rows)}</div>
    </div>
  </div>

  <div class="card">
    <h2>Timeline</h2>
    <div class="small">A simple ManicTime-style view of the current day: lanes by process, with idle in its own lane. Hover over a bar for details.</div>
    {timeline_svg}
    <div class="legend">{legend_html}</div>
  </div>

  <div class="card">
    <h2>Daily totals</h2>
    <div class="scroll">
      <table>
        <thead><tr><th>Date</th><th>Active</th><th>Idle</th></tr></thead>
        <tbody>{''.join(daily_rows) or '<tr><td colspan="3">No data yet.</td></tr>'}</tbody>
      </table>
    </div>
  </div>

  <div class="grid">
    <div class="card">
      <h2>Per-process totals</h2>
      <div class="scroll">
        <table>
          <thead><tr><th>Process</th><th>Active time</th></tr></thead>
          <tbody>{''.join(proc_rows) or '<tr><td colspan="2">No active data yet.</td></tr>'}</tbody>
        </table>
      </div>
    </div>

    <div class="card" style="grid-column: span 2;">
      <h2>Per-window totals</h2>
      <div class="scroll">
        <table>
          <thead><tr><th>Window</th><th>Active time</th></tr></thead>
          <tbody>{''.join(window_rows) or '<tr><td colspan="2">No active data yet.</td></tr>'}</tbody>
        </table>
      </div>
    </div>
  </div>

  <div class="card">
    <h2>Recent segments</h2>
    <div class="scroll">
      <table>
        <thead>
          <tr>
            <th>Kind</th>
            <th>Process</th>
            <th>Window title</th>
            <th>Start</th>
            <th>End</th>
            <th>Duration</th>
          </tr>
        </thead>
        <tbody>{''.join(recent_rows) or '<tr><td colspan="6">No data yet.</td></tr>'}</tbody>
      </table>
    </div>
  </div>
</div>
</body>
</html>
"""
        _, _, report_html = self.day_paths(target_day)
        report_html.write_text(report, encoding="utf-8")

    def summarize_console(self, rows: list[dict]) -> None:
        today = self.current_day_str()
        active_today = 0
        idle_today = 0
        for row in rows:
            start_iso = row.get("start_iso")
            try:
                start_day = date_str(parse_iso(start_iso))
            except Exception:
                continue
            dur = int(row.get("duration_seconds") or 0)
            if start_day == today:
                if row.get("kind") == "active":
                    active_today += dur
                elif row.get("kind") == "idle":
                    idle_today += dur

        print()
        print("Summary")
        print("-------")
        print(f"Today's active time: {fmt_hms(active_today)}")
        print(f"Today's idle time:   {fmt_hms(idle_today)}")
        segments_jsonl, segments_csv, report_html = self.current_paths()
        print(f"JSONL log:           {segments_jsonl}")
        print(f"CSV log:             {segments_csv}")
        print(f"HTML report:         {report_html}")

    def finalise_outputs(self) -> None:
        day = self.current_day_str()
        rows = self.load_segments(day=day)
        self.write_csv(rows, day=day)
        self.build_report(rows, day=day)
        self.summarize_console(rows)

    def maybe_refresh_report(self, force: bool = False) -> None:
        now_ts = time.time()
        if not force and (now_ts - self.last_report_build_ts) < self.report_every_seconds:
            return
        self.last_report_build_ts = now_ts
        day = self.current_day_str()
        rows = self.load_segments(day=day)

        live = None
        if self.current_segment is not None:
            start_ts = parse_iso(self.current_segment.start_iso)
            end_ts = now_local()
            dur = max(0, int((end_ts - start_ts).total_seconds()))
            live = asdict(self.current_segment)
            live["end_iso"] = iso_zoned(end_ts)
            live["duration_seconds"] = dur

        if live is not None:
            rows = rows + [live]

        self.write_csv(rows, day=day)
        self.build_report(rows, day=day)

    def shutdown(self) -> None:
        self.close_segment(now_local())
        self.finalise_outputs()

    def run(self) -> None:
        print("Tracking started.")
        print(f"Idle threshold: {self.idle_threshold_seconds} seconds")
        print(f"Polling every:  {self.poll_seconds} second(s)")
        print(f"Output folder:  {self.output_dir}")
        print("Daily rotation: one subfolder per day (YYYY-MM-DD)")
        day = self.current_day_str()
        segments_jsonl, segments_csv, report_html = self.day_paths(day)
        print(f"Today's HTML report: {report_html}")
        print(f"Report refresh: {self.report_every_seconds} second(s)")
        print("Press Ctrl+C to stop and build the final report.")
        self.maybe_refresh_report(force=True)
        try:
            while True:
                self.step()
                self.maybe_refresh_report()
                time.sleep(self.poll_seconds)
        except KeyboardInterrupt:
            print("\nStopping tracker...")
        finally:
            self.shutdown()


# =========================
# CLI
# =========================

def build_arg_parser() -> argparse.ArgumentParser:
    script_dir = Path(__file__).resolve().parent
    default_output = script_dir / "out"

    p = argparse.ArgumentParser(
        description="Windows idle/window time tracker using only the Python standard library."
    )
    p.add_argument(
        "--threshold-minutes",
        type=float,
        default=5.0,
        help="Idle threshold in minutes (default: 5).",
    )
    p.add_argument(
        "--poll-seconds",
        type=float,
        default=1.0,
        help="Polling interval in seconds (default: 1).",
    )
    p.add_argument(
        "--output-dir",
        type=Path,
        default=default_output,
        help=f"Directory for logs and report (default: {default_output}).",
    )
    p.add_argument(
        "--report-every-seconds",
        type=float,
        default=60.0,
        help="Rebuild the HTML/CSV report every N seconds while tracking (default: 300).",
    )
    return p

def main() -> int:
    args = build_arg_parser().parse_args()

    threshold_seconds = max(1, int(args.threshold_minutes * 60))
    poll_seconds = max(0.25, float(args.poll_seconds))
    output_dir = Path(os.path.expandvars(str(args.output_dir))).expanduser()

    tracker = Tracker(
        output_dir=output_dir,
        idle_threshold_seconds=threshold_seconds,
        poll_seconds=poll_seconds,
        report_every_seconds=max(10.0, float(args.report_every_seconds)),
    )
    tracker.run()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
