"""Conservative coverage for exactly compensating daily allocations."""

from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest

import check_octopus_kwh as probe
from sync_octopus_tado import (
    get_calibrated_cumulative_delta,
    reconcile_calibration_samples,
)


def readings():
    # Oct 29 read-only diagnostic deltas; absolute register offset removed.
    usage = [(24, "1"), (25, "1"), (26, "7.360"), (27, "6.139"), (28, "5.004")]
    register = [
        (23, "0"),
        (24, "2"),
        (25, "4"),
        (26, "18.720"),
        (27, "30.946"),
        (28, "41.006"),
    ]

    def items(values):
        return [
            {"value": value, "intervalEnd": f"2026-10-{day}T00:00:00+00:00"}
            for day, value in values
        ]

    return items(usage), items(register)


def calculate(intervals, cumulative, checkpoints=None):
    responses = [
        {
            "supplyPoint": {
                alias: {"importReadings": {"edges": [{"node": x} for x in records]}}
            }
        }
        for alias, records in [("m3", intervals), ("accumulation", cumulative)]
    ]
    with patch.object(probe, "obtain_token", return_value="test"), patch.object(
        probe, "graphql_request", side_effect=responses
    ):
        return get_calibrated_cumulative_delta(
            "test", "test", checkpoints or [date(2026, 10, 25)], date(2026, 10, 27)
        )


def test_observed_shift_reconciles_exactly_without_changing_factor(capsys):
    result = calculate(*readings())
    assert result == (11.143, date(2026, 10, 27), Decimal("2"), 5, date(2026, 10, 25))
    output = capsys.readouterr().out
    assert "Exactly reconciled interval ends: 2026-10-27 and 2026-10-28" in output
    assert "Independent calibration groups: 4" in output


def test_shifted_checkpoint_is_rewound_before_the_pair():
    result = calculate(
        *readings(), checkpoints=[date(2026, 10, 26), date(2026, 10, 25)]
    )
    assert result[0] == 11.143
    assert result[-1] == date(2026, 10, 25)


def test_no_safe_checkpoint_stops_submission():
    with pytest.raises(RuntimeError, match="no shared cumulative checkpoint"):
        calculate(*readings(), checkpoints=[date(2026, 10, 26)])


def test_small_uncancelled_difference_still_fails():
    intervals, cumulative = readings()
    cumulative[-1]["value"] = "41.007"
    with pytest.raises(RuntimeError, match="inconsistent ratios"):
        calculate(intervals, cumulative)


def test_three_days_with_pair_are_only_two_independent_groups():
    intervals, cumulative = readings()
    with pytest.raises(RuntimeError, match="independent calibration groups"):
        calculate(intervals[-3:], cumulative[-4:])


def test_old_and_future_samples_do_not_affect_recent_calibration():
    intervals, cumulative = readings()
    intervals.extend(
        [
            {"value": "1", "intervalEnd": "2026-06-23T00:00:00+00:00"},
            {"value": "1", "intervalEnd": "2026-10-29T00:00:00+00:00"},
        ]
    )
    cumulative.extend(
        [
            {"value": "-200", "intervalEnd": "2026-06-22T00:00:00+00:00"},
            {"value": "-100", "intervalEnd": "2026-06-23T00:00:00+00:00"},
            {"value": "100", "intervalEnd": "2026-10-29T00:00:00+00:00"},
        ]
    )
    result = calculate(
        intervals, cumulative, checkpoints=[date(2026, 6, 21), date(2026, 10, 25)]
    )
    assert result[0] == 11.143
    assert result[2:4] == (Decimal("2"), 5)


def test_nonadjacent_discrepancies_cannot_cancel():
    samples = [
        {
            "end_date": "2026-10-26",
            "ratio": Decimal("1.9"),
            "delta": Decimal("1.9"),
            "usage": Decimal("1"),
        },
        {
            "end_date": "2026-10-28",
            "ratio": Decimal("2.1"),
            "delta": Decimal("2.1"),
            "usage": Decimal("1"),
        },
    ]
    assert reconcile_calibration_samples(samples, Decimal("2")) == ([], [0, 1])
