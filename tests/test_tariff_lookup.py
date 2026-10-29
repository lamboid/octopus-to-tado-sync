"""Legacy product lookup and gas agreement selection regression coverage."""

from datetime import date
from unittest.mock import Mock

import pytest

import sync_octopus_tado as sync


@pytest.mark.parametrize(
    "tariff,product",
    [
        ("G-ISDFEDV1023SR1R-J", "ISDFEDV1023SR1R"),
        ("G-VAR-22-11-01-J", "VAR-22-11-01"),
        ("G-1R-VAR-22-11-01-J", "VAR-22-11-01"),
        ("E-1R-COSY-22-12-08-J", "COSY-22-12-08"),
        ("E-2R-VAR-22-11-01-J", "VAR-22-11-01"),
        ("G-1R-VAR-24-01-01", "VAR-24-01-01"),
    ],
)
def test_product_code_supports_optional_register_segment(tariff, product):
    assert sync.derive_product_code_from_tariff_code(tariff) == product


@pytest.mark.parametrize("tariff", ["G-1R-J", "G--J", "G-J", "unknown"])
def test_invalid_codes_do_not_generate_empty_product_urls(tariff):
    with pytest.raises(ValueError):
        sync.derive_product_code_from_tariff_code(tariff)


def account(agreements):
    return {
        "properties": [
            {
                "gas_meter_points": [
                    {
                        "mprn": "test",
                        "meters": [{"serial_number": "test"}],
                        "agreements": agreements,
                    }
                ],
                "electricity_meter_points": [
                    {"agreements": [{"tariff_code": "E-1R-COSY-22-12-08-J"}]}
                ],
            }
        ]
    }


def test_legacy_lookup_keeps_needed_history_and_ignores_electricity(monkeypatch):
    agreements = [
        {
            "tariff_code": "G-ISDFEDV1023SR1R-J",
            "valid_from": "2023-01-01",
            "valid_to": "2023-04-01",
        },
        {
            "tariff_code": "G-1R-VAR-22-11-01-J",
            "valid_from": "2023-04-01",
            "valid_to": None,
        },
    ]
    monkeypatch.setattr(
        sync, "get_octopus_account_details", Mock(return_value=account(agreements))
    )
    lookup = Mock(
        side_effect=[
            [
                {
                    "value_inc_vat": 10.29525,
                    "valid_from": "2023-01-01",
                    "valid_to": "2023-03-31T23:00:00Z",
                }
            ],
            [{"value_inc_vat": 7, "valid_from": "2023-04-01", "valid_to": None}],
        ]
    )
    monkeypatch.setattr(sync, "fetch_paginated_results", lookup)
    periods = sync.discover_octopus_tariff_periods("test", "test", "test", "test")
    assert len(periods) == 2
    urls = [call.args[0] for call in lookup.call_args_list]
    assert "/products/ISDFEDV1023SR1R/gas-tariffs/G-ISDFEDV1023SR1R-J/" in urls[0]
    assert "/products/VAR-22-11-01/gas-tariffs/G-1R-VAR-22-11-01-J/" in urls[1]
    assert all("COSY" not in url for url in urls)


def test_expired_history_is_skipped_before_lookup(monkeypatch):
    agreements = [
        {
            "tariff_code": "unresolvable-old-code",
            "valid_from": "2023-01-01",
            "valid_to": "2023-04-01",
        },
        {
            "tariff_code": "G-1R-VAR-22-11-01-J",
            "valid_from": "2023-04-01",
            "valid_to": None,
        },
    ]
    monkeypatch.setattr(
        sync, "get_octopus_account_details", Mock(return_value=account(agreements))
    )
    lookup = Mock(return_value=[{"value_inc_vat": 7, "valid_from": "2026-10-01"}])
    monkeypatch.setattr(sync, "fetch_paginated_results", lookup)
    periods = sync.discover_octopus_tariff_periods(
        "test", "test", "test", "test", since_date=date(2026, 9, 1)
    )
    assert len(periods) == 1
    lookup.assert_called_once()


def test_explicit_product_code_takes_precedence(monkeypatch):
    agreements = [
        {
            "tariff_code": "G-LEGACY-J",
            "product_code": "EXPLICIT",
            "valid_from": "2026-01-01",
            "valid_to": None,
        }
    ]
    monkeypatch.setattr(
        sync, "get_octopus_account_details", Mock(return_value=account(agreements))
    )
    lookup = Mock(return_value=[])
    monkeypatch.setattr(sync, "fetch_paginated_results", lookup)
    sync.discover_octopus_tariff_periods("test", "test", "test", "test")
    assert "/products/EXPLICIT/" in lookup.call_args.args[0]
