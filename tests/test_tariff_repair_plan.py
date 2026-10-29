"""Review-only plans preserve IDs and reject stale or unsupported evidence."""

import copy
from datetime import date

import pytest

import audit_tariffs as audit
import plan_tariff_repair as planner


def evidence(saved, expected):
    normalized = sorted(
        (audit.normalize_saved(x) for x in saved),
        key=lambda x: (x["start"], x["record_ref"]),
    )
    snapshot = {
        "saved_periods": normalized,
        "saved_snapshot_sha256": audit.fingerprint(normalized),
    }
    scenario = audit.build_audit(saved, expected, today=date(2026, 10, 29))
    scenario["payment_method_confirmation"] = {
        "method": "DIRECT_DEBIT",
        "confirmed_by": "user",
    }
    return snapshot, scenario


def saved(start, end, price, key):
    return {
        "id": key,
        "startDate": start,
        "endDate": end,
        "tariffInCents": price,
        "unit": "kWh",
    }


def source(start, end, price):
    return {
        "start_date": date.fromisoformat(start),
        "end_date": date.fromisoformat(end),
        "tariff_pence_per_kwh": price,
    }


def test_old_inclusive_quarter_boundaries_are_shrunk_before_missing_periods_are_added():
    snapshot, scenario = evidence(
        [
            saved("2023-01-01", "2023-04-01", 10, "a"),
            saved("2023-07-01", "2023-10-01", 8, "b"),
        ],
        [
            source("2023-01-01", "2023-03-31", 10),
            source("2023-04-01", "2023-06-30", 11),
            source("2023-07-01", "2023-09-30", 8),
            source("2023-10-01", "2023-12-31", 9),
        ],
    )
    plan = planner.build_plan(snapshot, scenario)
    assert plan["counts"] == {"keep": 0, "update": 2, "create": 2, "delete": 0}
    assert [x["method"] for x in plan["operations"]] == ["PUT", "PUT", "POST", "POST"]
    assert {x["record_ref"] for x in plan["operations"] if x["method"] == "PUT"} == {
        x["record_ref"] for x in snapshot["saved_periods"]
    }
    assert [
        x["payload"]["startDate"] for x in plan["operations"] if x["method"] == "POST"
    ] == ["2023-04-01", "2023-10-01"]


def test_correct_record_is_never_modified():
    snapshot, scenario = evidence(
        [
            saved("2026-01-01", "2026-01-02", 8, "wrong"),
            saved("2026-01-03", "2026-01-04", 9, "correct"),
        ],
        [source("2026-01-01", "2026-01-02", 7), source("2026-01-03", "2026-01-04", 9)],
    )
    plan = planner.build_plan(snapshot, scenario)
    assert plan["counts"] == {"keep": 1, "update": 1, "create": 0, "delete": 0}
    assert plan["operations"][0]["payload"]["tariffInCents"] == 7


@pytest.mark.parametrize("target", ["saved", "source"])
def test_tampered_evidence_is_rejected(target):
    snapshot, scenario = evidence(
        [saved("2026-01-01", "2026-01-02", 8, "wrong")],
        [source("2026-01-01", "2026-01-02", 7)],
    )
    if target == "saved":
        snapshot["saved_periods"][0]["pence_per_kwh"] = "100"
    else:
        scenario["source_periods"][0]["pence_per_kwh"] = "100"
    with pytest.raises(ValueError, match="fingerprint"):
        planner.build_plan(snapshot, scenario)


def test_unmatched_saved_record_is_not_deleted():
    snapshot, scenario = evidence(
        [saved("2026-01-01", "2026-01-02", 8, "outside")],
        [source("2026-02-01", "2026-02-02", 7)],
    )
    with pytest.raises(ValueError, match="retain all record IDs"):
        planner.build_plan(snapshot, scenario)


def test_plan_hash_covers_exact_operation_payloads():
    snapshot, scenario = evidence(
        [saved("2026-01-01", "2026-01-02", 8, "wrong")],
        [source("2026-01-01", "2026-01-02", 7)],
    )
    plan = planner.build_plan(snapshot, scenario)
    body = copy.deepcopy(plan)
    digest = body.pop("plan_sha256")
    assert audit.fingerprint(body) == digest
    body["operations"][0]["payload"]["tariffInCents"] = 10
    assert audit.fingerprint(body) != digest
