"""Read-only comparison and candidate repair plan; never mutates tariff data."""

import hashlib
import json
import os
from datetime import date, timedelta
from decimal import Decimal
from urllib.parse import urlparse

import sync_octopus_tado as sync


class ReadOnlyTadoSession(sync.TadoLoginSession):
    def send(self, request, **kwargs):
        url = urlparse(request.url)
        auth_post = (
            request.method == "POST"
            and url.hostname == "login.tado.com"
            and url.path in ("/oauth2/device_authorize", "/oauth2/token")
        )
        allowed_get = request.method == "GET" and url.hostname in (
            "my.tado.com",
            "energy-insights.tado.com",
            "hops.tado.com",
            "tariff-experience.tado.com",
            "login.tado.com",
        )
        if url.scheme != "https" or not (auth_post or allowed_get):
            raise RuntimeError("Read-only audit blocked a non-read Tado data request")
        return super().send(request, **kwargs)


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def normalize_saved(record):
    if record.get("unit") != "kWh" or not record.get("id"):
        raise ValueError("Saved tariff has an unsupported unit or missing ID")
    start = sync.parse_api_date(record.get("startDate"))
    end = sync.parse_api_date(record.get("endDate"))
    if start is None or (end is not None and end < start):
        raise ValueError("Saved tariff has invalid dates")
    pence = Decimal(str(record["tariffInCents"]))
    if not pence.is_finite() or pence < 0:
        raise ValueError("Saved tariff has invalid price")
    return {
        "record_ref": fingerprint(
            {
                "id": record["id"],
                "start": str(start),
                "end": str(end),
                "pence": str(pence),
            }
        )[:16],
        "start": str(start),
        "end": str(end) if end else None,
        "pence_per_kwh": str(pence),
    }


def normalize_expected(period):
    pence = Decimal(str(period["tariff_pence_per_kwh"]))
    if not pence.is_finite() or pence < 0:
        raise ValueError("Source tariff has invalid price")
    return {
        "start": str(period["start_date"]),
        "end": str(period["end_date"]) if period["end_date"] else None,
        "pence_per_kwh": str(pence),
    }


def daily_map(periods, horizon):
    days = {}
    for index, period in enumerate(periods):
        start = date.fromisoformat(period["start"])
        end = min(
            date.fromisoformat(period["end"]) if period["end"] else horizon, horizon
        )
        if end - start > timedelta(days=10000):
            raise ValueError("Tariff audit range exceeds bounded history")
        while start <= end:
            days.setdefault(start, []).append(index)
            start += timedelta(days=1)
    return days


def build_audit(saved_records, expected_periods, today=None):
    today = today or date.today()
    saved = sorted(
        (normalize_saved(record) for record in saved_records),
        key=lambda x: (x["start"], x["record_ref"]),
    )
    expected = sorted(
        (normalize_expected(period) for period in expected_periods),
        key=lambda x: x["start"],
    )
    if not expected:
        raise ValueError("No authoritative Octopus tariff periods returned")
    finite_ends = [date.fromisoformat(p["end"]) for p in expected + saved if p["end"]]
    horizon = max(
        [today]
        + finite_ends
        + [date.fromisoformat(p["start"]) for p in expected + saved]
    )
    source_days = daily_map(expected, horizon)
    stored_days = daily_map(saved, horizon)
    if any(len(items) != 1 for items in source_days.values()):
        raise ValueError("Authoritative source periods overlap")
    kept, candidates = [], []
    for index, record in enumerate(saved):
        reasons = set()
        for day, indices in stored_days.items():
            if index not in indices:
                continue
            if len(indices) > 1:
                reasons.add("overlaps_saved_record")
            source = source_days.get(day)
            if not source:
                reasons.add("outside_source_validity")
            elif abs(
                Decimal(record["pence_per_kwh"])
                - Decimal(expected[source[0]]["pence_per_kwh"])
            ) > Decimal("0.00001"):
                reasons.add("price_or_day_mismatch")
        # An open-ended saved period needs an open-ended authoritative rate.
        if record["end"] is None and not any(p["end"] is None for p in expected):
            reasons.add("outside_source_validity")
        if reasons:
            candidates.append(dict(record, reasons=sorted(reasons)))
        else:
            kept.append(record)
    kept_days = daily_map(kept, horizon)
    create = []
    for day in sorted(source_days):
        if day in kept_days:
            continue
        source = expected[source_days[day][0]]
        pence = source["pence_per_kwh"]
        if (
            create
            and create[-1]["pence_per_kwh"] == pence
            and date.fromisoformat(create[-1]["end"]) + timedelta(days=1) == day
        ):
            create[-1]["end"] = str(day)
        else:
            create.append({"start": str(day), "end": str(day), "pence_per_kwh": pence})
    # Preserve a source's genuinely open-ended final period in the candidate plan.
    if create and expected[-1]["end"] is None and create[-1]["end"] == str(horizon):
        create[-1]["end"] = None
    return {
        "mode": "read_only_candidate_plan_not_executed",
        "audit_date": str(today),
        "saved_snapshot_sha256": fingerprint(saved),
        "source_snapshot_sha256": fingerprint(expected),
        "counts": {
            "saved": len(saved),
            "source_periods": len(expected),
            "keep": len(kept),
            "replace_candidates": len(candidates),
            "create_candidates": len(create),
        },
        "source_periods": expected,
        "replace_candidates": candidates,
        "create_candidates": create,
        "limitations": [
            "Outside source validity means unverified, not automatically wrong.",
            "No record IDs or household identifiers are emitted.",
            "A supported edit/delete route must be verified before approving repair.",
            "Snapshot fingerprints and prices must be revalidated immediately before any approved repair.",
        ],
    }


def read_source_periods(api_key, account_number, mprn, serial):
    account = sync.get_octopus_account_details(api_key, account_number)
    agreements = sync.get_octopus_gas_agreements(account, mprn, serial)
    if not agreements:
        raise ValueError("No matching gas agreements for audit")
    periods, issues = [], []
    for agreement in agreements:
        tariff = agreement.get("tariff_code") or agreement.get("tariffCode")
        if not tariff:
            raise ValueError("Gas agreement has no tariff code")
        product = agreement.get(
            "product_code"
        ) or sync.derive_product_code_from_tariff_code(tariff)
        rates = sync.get_octopus_standard_unit_rates(api_key, product, tariff)
        try:
            periods.extend(sync.build_octopus_tariff_periods(agreement, rates))
        except (ValueError, RuntimeError) as error:
            # Emit only public product/rate facts, not account or meter data.
            start = sync.parse_api_date(agreement.get("valid_from"))
            end = sync.parse_api_date(agreement.get("valid_to"))
            relevant = []
            for rate in rates:
                rate_start = sync.parse_api_date(rate.get("valid_from"))
                rate_end = sync.parse_api_date(rate.get("valid_to"))
                if (end is None or rate_start is None or rate_start < end) and (
                    start is None or rate_end is None or rate_end > start
                ):
                    relevant.append(
                        {
                            key: rate.get(key)
                            for key in (
                                "valid_from",
                                "valid_to",
                                "value_inc_vat",
                                "payment_method",
                            )
                        }
                    )
            issues.append(
                {
                    "product_code": product,
                    "tariff_code": tariff,
                    "agreement_from": str(start),
                    "agreement_to": str(end) if end else None,
                    "reason": type(error).__name__,
                    "rate_candidates": relevant,
                }
            )
    return periods, issues


def main():
    required = (
        "TADO_EMAIL",
        "TADO_PASSWORD",
        "OCTOPUS_API_KEY",
        "OCTOPUS_ACCOUNT_NUMBER",
        "OCTOPUS_MPRN",
        "OCTOPUS_GAS_SERIAL",
    )
    if any(not os.environ.get(name) for name in required):
        raise ValueError(
            "Missing configured audit credentials; no data audit attempted"
        )
    tado = sync.tado_login(
        os.environ["TADO_EMAIL"],
        os.environ["TADO_PASSWORD"],
        session=ReadOnlyTadoSession(),
    )
    print("Tariff audit phase: reading saved Tado tariffs")
    data = sync.call_tado_method(tado, "get_eiq_tariffs", "getEIQTariffs")
    if isinstance(data, dict) and "tariffs" in data and not data.get("code"):
        records = data["tariffs"]
    elif isinstance(data, list):
        records = data
    else:
        raise ValueError("Tado did not return readable tariff history")
    if not isinstance(records, list):
        raise ValueError("Tado tariff history is not a list")
    print("Tariff audit phase: reading complete Octopus gas tariff history")
    expected, issues = read_source_periods(
        os.environ["OCTOPUS_API_KEY"],
        os.environ["OCTOPUS_ACCOUNT_NUMBER"],
        os.environ["OCTOPUS_MPRN"],
        os.environ["OCTOPUS_GAS_SERIAL"],
    )
    if issues:
        normalized = sorted(
            (normalize_saved(record) for record in records),
            key=lambda x: (x["start"], x["record_ref"]),
        )
        report = {
            "mode": "source_ambiguity_blocks_repair_plan",
            "counts": {"saved": len(records), "source_issues": len(issues)},
            "saved_snapshot_sha256": fingerprint(normalized),
            "saved_periods": normalized,
            "source_issues": issues,
            "unambiguous_source_periods": [
                normalize_expected(period) for period in expected
            ],
        }
    else:
        report = build_audit(records, expected)
    sync.append_github_step_summary(
        "Read-only tariff repair audit", list(report["counts"].items())
    )
    print(
        "TARIFF_AUDIT_JSON=" + json.dumps(report, sort_keys=True, separators=(",", ":"))
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # A request exception can contain account identifiers; do not echo it.
        print("Tariff audit stopped safely: " + type(error).__name__)
        if str(error).startswith(
            (
                "Intraday gas tariff",
                "Overlapping Octopus",
                "Ambiguous Octopus",
                "Europe/London",
                "Saved tariff",
                "Authoritative source",
            )
        ):
            print(str(error))
        raise SystemExit(1)
