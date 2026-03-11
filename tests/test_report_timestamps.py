from pathlib import Path

import ttrack


def build_report(tmp_path: Path, rows: list[dict], day: str = "2026-03-10") -> str:
    tracker = ttrack.Tracker(
        output_dir=tmp_path,
        idle_threshold_seconds=300,
        poll_seconds=1,
        report_every_seconds=60,
    )
    tracker.build_report(rows, day=day)
    return (tmp_path / day / "report.html").read_text(encoding="utf-8")


def test_recent_segments_display_human_readable_timestamps(tmp_path: Path) -> None:
    html = build_report(
        tmp_path,
        rows=[
            {
                "kind": "active",
                "process_name": "python.exe",
                "window_title": "Editor",
                "start_iso": "2026-03-10T14:15:16-04:00",
                "end_iso": "2026-03-10T14:45:16-04:00",
                "duration_seconds": 1800,
                "pid": 1234,
            }
        ],
    )

    assert "2026-03-10 14:15:16" in html
    assert "2026-03-10 14:45:16" in html
    assert "2026-03-10T14:15:16-04:00" not in html
    assert "2026-03-10T14:45:16-04:00" not in html
