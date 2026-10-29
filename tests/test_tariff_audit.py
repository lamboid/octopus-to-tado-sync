"""The audit cannot write tariff data and preserves matching saved periods."""

import json
from datetime import date
from unittest.mock import Mock, patch

import pytest
import requests

import audit_tariffs as audit


@pytest.mark.parametrize(
    "method,url,allowed",
    [
        ("POST", "https://login.tado.com/oauth2/token", True),
        ("POST", "https://login.tado.com/oauth2/device_authorize", True),
        ("GET", "https://energy-insights.tado.com/api/homes/test/tariffs", True),
        ("POST", "https://energy-insights.tado.com/api/homes/test/tariffs", False),
        (
            "DELETE",
            "https://energy-insights.tado.com/api/homes/test/tariffs/test",
            False,
        ),
        ("PUT", "https://energy-insights.tado.com/api/homes/test/tariffs/test", False),
        ("GET", "https://untrusted.example/", False),
        ("GET", "http://my.tado.com/api/v2/me", False),
    ],
)
def test_readonly_transport(method, url, allowed):
    prepared = requests.Request(method, url).prepare()
    with patch.object(requests.Session, "send", return_value=Mock()) as send:
        session = audit.ReadOnlyTadoSession()
        if allowed:
            session.send(prepared)
            send.assert_called_once()
        else:
            with pytest.raises(RuntimeError, match="blocked"):
                session.send(prepared)
            send.assert_not_called()


def saved(start, end, price=7, record_id="private-record-id"):
    return {
        "id": record_id,
        "homeId": "private-household",
        "startDate": start,
        "endDate": end,
        "tariffInCents": price,
        "unit": "kWh",
    }


def source(start, end, price=7):
    return {
        "start_date": date.fromisoformat(start),
        "end_date": date.fromisoformat(end) if end else None,
        "tariff_pence_per_kwh": price,
        "unit": "kWh",
    }


def test_identical_records_are_kept_with_no_repair():
    result = audit.build_audit(
        [saved("2026-01-01", "2026-01-03")], [source("2026-01-01", "2026-01-03")]
    )
    assert result["counts"]["keep"] == 1
    assert result["replace_candidates"] == result["create_candidates"] == []
    assert "private-record-id" not in json.dumps(result)
    assert "private-household" not in json.dumps(result)


def test_inclusive_boundary_and_gap_produce_exact_candidates():
    result = audit.build_audit(
        [saved("2023-01-01", "2023-04-01", 10)],
        [
            source("2023-01-01", "2023-03-31", 10),
            source("2023-04-01", "2023-06-30", 11),
        ],
    )
    assert result["counts"]["replace_candidates"] == 1
    assert result["create_candidates"] == [
        {"start": "2023-01-01", "end": "2023-03-31", "pence_per_kwh": "10"},
        {"start": "2023-04-01", "end": "2023-06-30", "pence_per_kwh": "11"},
    ]


def test_extension_beyond_available_source_is_reviewed_not_replayed():
    result = audit.build_audit(
        [saved("2026-10-29", "2027-03-29", 11)],
        [source("2026-10-29", "2026-10-29", 11)],
        today=date(2026, 10, 29),
    )
    assert result["replace_candidates"][0]["reasons"] == ["outside_source_validity"]
    assert result["create_candidates"][0]["end"] == "2026-10-29"


def test_correct_split_records_are_preserved_and_rounding_is_tolerated():
    result = audit.build_audit(
        [
            saved("2026-01-01", "2026-01-01", 7.000005),
            saved("2026-01-02", "2026-01-03", 7, "another"),
        ],
        [source("2026-01-01", "2026-01-03", 7)],
    )
    assert result["counts"]["keep"] == 2
    assert not result["create_candidates"]


def test_source_overlaps_stop_the_plan():
    with pytest.raises(ValueError, match="overlap"):
        audit.build_audit(
            [], [source("2026-01-01", "2026-01-03"), source("2026-01-03", "2026-01-04")]
        )


def test_open_ended_source_remains_open_in_candidate_plan():
    result = audit.build_audit([], [source("2026-01-01", None)], today=date(2026, 1, 3))
    assert result["create_candidates"] == [
        {"start": "2026-01-01", "end": None, "pence_per_kwh": "7"}
    ]


def test_saved_overlap_marks_both_records():
    result = audit.build_audit(
        [
            saved("2026-01-01", "2026-01-02"),
            saved("2026-01-02", "2026-01-03", record_id="other"),
        ],
        [source("2026-01-01", "2026-01-03")],
    )
    assert result["counts"]["replace_candidates"] == 2
    assert all(
        "overlaps_saved_record" in record["reasons"]
        for record in result["replace_candidates"]
    )
