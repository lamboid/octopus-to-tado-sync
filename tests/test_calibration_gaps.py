"""Regression coverage for missing cumulative daily readings."""

from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest

import check_octopus_kwh as probe
from sync_octopus_tado import get_calibrated_cumulative_delta


def datasets():
    # Shape and deltas from the October 12 diagnostic; register offset removed.
    usage = [(27, "1.158"), (28, "0.983"), (29, "1.035"), (30, "1.080")]
    registers = [(26, "0"), (27, "2.316"), (28, "4.282"), (30, "8.512")]
    intervals = [
        {"value": value, "intervalEnd": f"2026-09-{day}T00:00:00+01:00"}
        for day, value in usage
    ]
    cumulative = [
        {"value": value, "intervalEnd": f"2026-09-{day}T01:00:00+01:00"}
        for day, value in registers
    ]
    intervals.append({"value": "1.026", "intervalEnd": "2026-10-01T00:00:00+01:00"})
    cumulative.append({"value": "10.564", "intervalEnd": "2026-10-01T01:00:00+01:00"})
    return intervals, cumulative


def responses(intervals, cumulative):
    return [
        {
            "supplyPoint": {
                alias: {"importReadings": {"edges": [{"node": item} for item in items]}}
            }
        }
        for alias, items in [("m3", intervals), ("accumulation", cumulative)]
    ]


def test_missing_day_is_not_compared_with_single_day_usage():
    intervals, cumulative = datasets()
    assert probe.accumulation_delta_ratios(intervals, cumulative) == [Decimal("2")] * 3


def test_gap_preserves_full_checkpoint_delta():
    intervals, cumulative = datasets()
    with patch.object(probe, "obtain_token", return_value="test"), patch.object(
        probe, "graphql_request", side_effect=responses(intervals, cumulative)
    ):
        result = get_calibrated_cumulative_delta(
            "test", "test", [date(2026, 9, 25)], date(2026, 9, 30)
        )
    assert result == (5.282, date(2026, 9, 30), Decimal("2"), 3, date(2026, 9, 25))


@pytest.mark.parametrize("problem", ["too_few", "unstable"])
def test_gap_fix_preserves_calibration_guards(problem, capsys, tmp_path, monkeypatch):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    intervals, cumulative = datasets()
    if problem == "too_few":
        cumulative.pop()
    else:
        cumulative[-1]["value"] = "11.564"
    with patch.object(probe, "obtain_token", return_value="test"), patch.object(
        probe, "graphql_request", side_effect=responses(intervals, cumulative)
    ), pytest.raises(RuntimeError, match="not stable"):
        get_calibrated_cumulative_delta(
            "test", "test", [date(2026, 9, 25)], date(2026, 9, 30)
        )
    reason = (
        "insufficient matched days" if problem == "too_few" else "inconsistent ratios"
    )
    assert reason in capsys.readouterr().out
    assert reason in summary.read_text()
    assert "Median ratio" in summary.read_text()
    assert "2026-09-28" in summary.read_text()


def test_calibration_diagnostics_show_dst_elapsed_hours():
    intervals = [{"value": "1", "intervalEnd": "2026-10-26T00:00:00+00:00"}]
    cumulative = [
        {"value": "10", "intervalEnd": "2026-10-25T00:00:00+01:00"},
        {"value": "12", "intervalEnd": "2026-10-26T00:00:00+00:00"},
    ]
    assert probe.accumulation_delta_samples(intervals, cumulative) == [
        {"end_date": "2026-10-26", "ratio": Decimal("2"), "elapsed_hours": 25}
    ]
    assert probe.accumulation_delta_ratios(intervals, cumulative) == [Decimal("2")]
