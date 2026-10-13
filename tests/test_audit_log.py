"""The audit log stays readable, and keeps new events, after a crash mid-write."""

import logging

import pytest

from app.audit import AuditLog

PARTIAL = '{"ts": "2026-10-13T10:00:00", "event": "st'  # cut off: no brace, no newline


def _crashed_log(tmp_path) -> AuditLog:
    log = AuditLog(tmp_path / "audit.jsonl")
    log.write("run_start", run_id="r1")
    with log.path.open("a", encoding="utf-8") as handle:
        handle.write(PARTIAL)
    return log


def test_event_after_a_crash_is_kept_whole(tmp_path):
    """Security: a crash must not also swallow the next session's events."""
    log = _crashed_log(tmp_path)
    log.write("run_start", run_id="r2")
    assert [e["run_id"] for e in log.read()] == ["r1", "r2"]
    lines = log.path.read_text(encoding="utf-8").splitlines()
    assert lines[1] == PARTIAL  # sealed on its own line, not merged


def test_damaged_line_is_skipped_with_line_number_only(tmp_path, caplog):
    log = _crashed_log(tmp_path)
    log.write("run_start", run_id="r2")
    with caplog.at_level(logging.WARNING, logger="paios.audit"):
        log.read()
    record = [r for r in caplog.records if r.getMessage() == "audit_damaged_lines"][0]
    assert record.event == {"lines": [2]}
    assert "2026-10-13T10:00:00" not in caplog.text  # the damaged content is never logged


def test_damaged_last_line_is_skipped(tmp_path):
    log = _crashed_log(tmp_path)
    assert [e["run_id"] for e in log.read()] == ["r1"]


def test_strict_read_raises_on_damage(tmp_path):
    log = _crashed_log(tmp_path)
    with pytest.raises(ValueError, match="line 2"):
        log.read(strict=True)


def test_intact_log_gets_no_extra_newlines(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    for i in range(3):
        log.write("step", index=i)
    text = log.path.read_text(encoding="utf-8")
    assert text.count("\n") == 3 and "\n\n" not in text
    assert [e["index"] for e in log.read(strict=True)] == [0, 1, 2]


def test_missing_file_reads_as_empty(tmp_path):
    assert AuditLog(tmp_path / "audit.jsonl").read() == []
