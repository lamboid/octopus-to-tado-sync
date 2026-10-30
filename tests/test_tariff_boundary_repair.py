"""The boundary repair is exact, single-use, and independently verified."""

import copy
from datetime import date
from unittest.mock import Mock, patch

import pytest
import requests

import audit_tariffs as audit
import repair_tariff_boundary as repair
import sync_octopus_tado as sync


@pytest.fixture
def evidence(monkeypatch):
    record = {
        "id": "test-record",
        "unit": "kWh",
        "startDate": "2026-10-29",
        "endDate": "2027-03-29",
        "tariffInCents": 11.2875,
    }
    monkeypatch.setattr(
        repair, "REVIEWED_REF", audit.normalize_saved(record)["record_ref"]
    )
    source = [
        {
            "start_date": date(2026, 10, 29),
            "end_date": date(2026, 10, 29),
            "tariff_pence_per_kwh": 11.2875,
            "unit": "kWh",
        }
    ]
    return record, source


def test_review_does_not_write_and_apply_verifies_exact_change(evidence):
    record, source = evidence
    tado, session = Mock(), repair.BoundaryRepairSession()
    tado.get_eiq_tariffs.return_value = [record]
    assert (
        repair.run_repair(tado, session, source)["status"] == "review_only_not_executed"
    )
    tado._http.request.assert_not_called()
    tado.get_eiq_tariffs.side_effect = [
        [record],
        [record],
        [dict(record, endDate="2026-10-29")],
    ]
    report = repair.run_repair(tado, session, source, apply=True)
    assert report["status"] == "applied_and_independently_verified"
    request = tado._http.request.call_args.args[0]
    assert request.command == "tariffs/test-record"
    assert request.action == "PUT"
    assert request.payload == {
        "unit": "kWh",
        "startDate": "2026-10-29",
        "endDate": "2026-10-29",
        "tariffInCents": 11.2875,
    }
    assert session.approved_url is None


@pytest.mark.parametrize("change", ["saved", "source_price", "source_end", "ambiguous"])
def test_stale_or_unconfirmed_evidence_cannot_write(evidence, change):
    record, source = copy.deepcopy(evidence)
    if change == "saved":
        record["endDate"] = "2027-03-28"
    elif change == "source_price":
        source[0]["tariff_pence_per_kwh"] = 12
    elif change == "source_end":
        source[0]["end_date"] = date(2026, 10, 30)
    else:
        source.append(dict(source[0]))
    tado = Mock()
    tado.get_eiq_tariffs.return_value = [record]
    with pytest.raises(ValueError):
        repair.run_repair(tado, repair.BoundaryRepairSession(), source, apply=True)
    tado._http.request.assert_not_called()


def test_drift_before_write_and_unexpected_readback_stop(evidence):
    record, source = evidence
    tado = Mock()
    tado.get_eiq_tariffs.side_effect = [[record], []]
    with pytest.raises(ValueError, match="changed during review"):
        repair.run_repair(tado, repair.BoundaryRepairSession(), source, apply=True)
    tado._http.request.assert_not_called()
    tado.get_eiq_tariffs.side_effect = [[record], [record], [record]]
    with pytest.raises(RuntimeError, match="read-back differs"):
        repair.run_repair(tado, repair.BoundaryRepairSession(), source, apply=True)
    tado._http.request.assert_called_once()


def test_write_transport_is_single_use_and_unknown_outcomes_cannot_replay():
    session = repair.BoundaryRepairSession()
    url = "https://energy-insights.tado.com/api/homes/test/tariffs/test"
    payload = {"endDate": "2026-10-29"}
    request = requests.Request("PUT", url, json=payload).prepare()
    session.approved_url, session.approved_payload = url, payload
    with patch.object(
        requests.Session, "send", side_effect=requests.ConnectionError
    ) as send:
        with pytest.raises(RuntimeError, match="outcome unknown"):
            session.send(request)
        with pytest.raises(RuntimeError, match="unapproved"):
            session.send(request)
    send.assert_called_once_with(session, request, timeout=(10, 30), allow_redirects=False)


@pytest.mark.parametrize("end", ["2027-03-29", None])
def test_entire_batch_preflight_stops_before_any_tariff_post(monkeypatch, end):
    tado = Mock()
    tado.get_eiq_tariffs.return_value = [{"startDate": "2026-10-29", "endDate": end}]
    periods = [
        {
            "start_date": date(2026, 10, day),
            "end_date": date(2026, 10, day),
            "tariff_pence_per_kwh": 11,
            "unit": "kWh",
        }
        for day in (30, 31)
    ]
    monkeypatch.setattr(
        sync, "discover_octopus_tariff_periods", Mock(return_value=periods)
    )
    with pytest.raises(RuntimeError, match="explicit boundary repair required"):
        sync.sync_octopus_tariffs_to_tado(tado, "test", "test", "test", "test")
    tado.set_eiq_tariff.assert_not_called()


def test_corrected_boundary_allows_next_daily_tariff(monkeypatch):
    tado = Mock()
    tado.get_eiq_tariffs.return_value = [
        {
            "startDate": "2026-10-29",
            "endDate": "2026-10-29",
        }
    ]
    period = {
        "start_date": date(2026, 10, 30),
        "end_date": date(2026, 10, 30),
        "tariff_pence_per_kwh": 11,
        "unit": "kWh",
    }
    monkeypatch.setattr(
        sync, "discover_octopus_tariff_periods", Mock(return_value=[period])
    )
    tado.set_eiq_tariff.return_value = {"id": "new"}
    assert (
        len(sync.sync_octopus_tariffs_to_tado(tado, "test", "test", "test", "test"))
        == 1
    )
