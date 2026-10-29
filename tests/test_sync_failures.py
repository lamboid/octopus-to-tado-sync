"""Failures must remain visible without repeating a meter submission."""

from argparse import Namespace
from unittest.mock import Mock

import pytest

import sync_octopus_tado as sync


@pytest.mark.parametrize("account", ["A-TEST", None])
def test_tariff_failure_propagates_after_meter_submission(
    monkeypatch, tmp_path, account
):
    args = Namespace(
        tado_email="test",
        tado_password="test",
        octopus_api_key="test",
        mprn="test",
        gas_serial_number="test",
        octopus_account_number=account,
        octopus_consumption_unit="m3",
        tado_reading_unit="kwh",
        m3_to_kwh_factor=sync.DEFAULT_M3_TO_KWH_FACTOR,
        meter_source="calibrated-cumulative",
        dry_run_meter=False,
        update_tariff=True,
    )
    monkeypatch.setattr(sync, "parse_args", lambda: args)
    monkeypatch.setattr(sync, "tado_login", Mock(return_value=object()))
    monkeypatch.setattr(
        sync,
        "get_meter_reading_total_consumption",
        Mock(return_value=(123, "2026-10-25")),
    )
    submit = Mock()
    monkeypatch.setattr(sync, "send_reading_to_tado_client", submit)
    tariff = Mock(side_effect=RuntimeError("unit-rate HTTP 404"))
    monkeypatch.setattr(sync, "sync_octopus_tariffs_to_tado", tariff)
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    with pytest.raises(
        RuntimeError, match="Tariff sync failed" if account else "account number"
    ):
        sync.main()
    submit.assert_called_once()
    if account:
        tariff.assert_called_once()
        assert "not rolled back" in summary.read_text()
    else:
        tariff.assert_not_called()


def test_rate_failure_identifies_public_tariff_not_account(monkeypatch):
    monkeypatch.setattr(
        sync,
        "fetch_paginated_results",
        Mock(side_effect=RuntimeError("Status code: 404")),
    )
    with pytest.raises(
        RuntimeError, match="product TEST-PRODUCT, tariff G-1R-TEST-PRODUCT-J"
    ):
        sync.get_octopus_standard_unit_rates(
            "private-api-key", "TEST-PRODUCT", "G-1R-TEST-PRODUCT-J"
        )
