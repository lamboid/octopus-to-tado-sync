"""UK calendar boundaries and explicit Tado failure responses."""

from datetime import date
from unittest.mock import Mock

import pytest

import sync_octopus_tado as sync


@pytest.mark.parametrize(
    "timestamp,day",
    [
        ("2023-03-31T23:00:00Z", date(2023, 4, 1)),
        ("2025-03-30T23:00:00Z", date(2025, 3, 31)),
        ("2026-03-29T23:00:00Z", date(2026, 3, 30)),
        ("2026-10-25T00:00:00+01:00", date(2026, 10, 25)),
        ("2026-10-26T00:00:00Z", date(2026, 10, 26)),
    ],
)
def test_uk_boundaries(timestamp, day):
    assert sync.parse_tariff_boundary(timestamp) == day


def test_exclusive_agreement_and_rate_ends_are_clipped_to_inclusive_days():
    agreement = {"valid_from": "2023-01-01", "valid_to": "2023-04-01T00:00:00+01:00"}
    rates = [
        {"value_inc_vat": 10, "valid_from": "2022-01-01", "valid_to": "2023-01-01"},
        {
            "value_inc_vat": 11,
            "valid_from": "2023-01-01",
            "valid_to": "2023-03-31T23:00:00Z",
        },
        {"value_inc_vat": 12, "valid_from": "2023-03-31T23:00:00Z", "valid_to": None},
    ]
    assert sync.build_octopus_tariff_periods(agreement, rates) == [
        {
            "start_date": date(2023, 1, 1),
            "end_date": date(2023, 3, 31),
            "tariff_pence_per_kwh": 11,
            "unit": "kWh",
        }
    ]


def test_finite_rate_does_not_extend_to_future_agreement_end():
    periods = sync.build_octopus_tariff_periods(
        {"valid_from": "2026-01-01", "valid_to": "2027-03-30"},
        [{"value_inc_vat": 11, "valid_from": "2026-10-29", "valid_to": "2026-10-30"}],
    )
    assert periods[0]["end_date"] == date(2026, 10, 29)


def test_equal_rates_do_not_fill_a_gap():
    periods = sync.build_octopus_tariff_periods(
        {"valid_from": "2026-01-01", "valid_to": None},
        [
            {"value_inc_vat": 7, "valid_from": "2026-01-01", "valid_to": "2026-01-02"},
            {"value_inc_vat": 7, "valid_from": "2026-01-03", "valid_to": "2026-01-04"},
        ],
    )
    assert len(periods) == 2


def test_intraday_changes_are_not_guessed_as_daily_prices():
    with pytest.raises(ValueError, match="Intraday"):
        sync.parse_tariff_boundary("2026-10-29T12:00:00Z")


def test_ambiguous_rates_do_not_silently_select_a_payment_method():
    with pytest.raises(RuntimeError, match="Ambiguous"):
        sync.build_octopus_tariff_periods(
            {"valid_from": "2026-01-01", "valid_to": None},
            [
                {"value_inc_vat": value, "valid_from": "2026-01-01", "valid_to": None}
                for value in (7, 8)
            ],
        )


def test_overlapping_agreements_are_rejected_before_tado_writes(monkeypatch):
    agreements = [
        {"tariff_code": "G-1R-TEST-J", "valid_from": start, "valid_to": end}
        for start, end in [("2026-01-01", "2026-01-05"), ("2026-01-03", None)]
    ]
    account = {
        "properties": [
            {"gas_meter_points": [{"mprn": "test", "agreements": agreements}]}
        ]
    }
    monkeypatch.setattr(sync, "get_octopus_account_details", Mock(return_value=account))
    monkeypatch.setattr(
        sync,
        "get_octopus_standard_unit_rates",
        Mock(
            return_value=[
                {"value_inc_vat": 7, "valid_from": "2026-01-01", "valid_to": None}
            ]
        ),
    )
    tado = Mock()
    tado.get_eiq_tariffs.return_value = {"tariffs": []}
    with pytest.raises(RuntimeError, match="Overlapping"):
        sync.sync_octopus_tariffs_to_tado(tado, "test", "test", "test", "test")
    tado.set_eiq_tariff.assert_not_called()


@pytest.mark.parametrize(
    "response", [{"code": "conflicting_tariff_dates"}, {}, None, {"message": "error"}]
)
def test_rejection_stops_before_next_submission(monkeypatch, tmp_path, response):
    tado = Mock()
    tado.get_eiq_tariffs.return_value = {"tariffs": []}
    tado.set_eiq_tariff.side_effect = [
        {"id": "accepted"},
        response,
        {"id": "must-not-run"},
    ]
    periods = [
        {
            "start_date": date(2026, 10, day),
            "end_date": date(2026, 10, day),
            "tariff_pence_per_kwh": 7,
            "unit": "kWh",
        }
        for day in (1, 2, 3)
    ]
    monkeypatch.setattr(
        sync, "discover_octopus_tariff_periods", Mock(return_value=periods)
    )
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    with pytest.raises(RuntimeError, match="submission failed on 2026-10-02"):
        sync.sync_octopus_tariffs_to_tado(tado, "test", "test", "test", "test")
    assert tado.set_eiq_tariff.call_count == 2
    assert "Accepted before stop | 1" in summary.read_text()


@pytest.mark.parametrize(
    "response", [{"code": "unauthorized"}, None, {"unexpected": []}]
)
def test_bad_history_does_not_trigger_blind_backfill(monkeypatch, response):
    tado = Mock()
    tado.get_eiq_tariffs.return_value = response
    discover = Mock()
    monkeypatch.setattr(sync, "discover_octopus_tariff_periods", discover)
    with pytest.raises(RuntimeError, match="refusing blind tariff backfill"):
        sync.sync_octopus_tariffs_to_tado(tado, "test", "test", "test", "test")
    discover.assert_not_called()
    tado.set_eiq_tariff.assert_not_called()
