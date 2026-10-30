"""Review or apply the single approved 29 October 2026 tariff end-date repair."""

import argparse
import json
import os
from datetime import date
from decimal import Decimal
from urllib.parse import quote

import requests
from PyTado.http import Action, Endpoint, TadoRequest

import audit_tariffs as audit
import sync_octopus_tado as sync

REVIEWED_REF = "00cbe756012d87db"
REVIEWED_START = "2026-10-29"
REVIEWED_END = "2027-03-29"
CORRECTED_END = "2026-10-29"
REVIEWED_PRICE = Decimal("11.2875")


class BoundaryRepairSession(audit.ReadOnlyTadoSession):
    """Permit one exact armed PUT; never replay a failed/unknown write."""

    approved_url = None
    approved_payload = None

    def send(self, request, **kwargs):
        if request.method != "PUT":
            return super().send(request, **kwargs)
        if (
            self.approved_url is None
            or request.url != self.approved_url
            or json.loads(request.body) != self.approved_payload
        ):
            raise RuntimeError("Boundary repair blocked an unapproved write")
        self.approved_url = None
        kwargs["timeout"] = (10, 30)
        kwargs["allow_redirects"] = False
        try:
            response = requests.Session.send(self, request, **kwargs)
        except requests.RequestException:
            raise RuntimeError(
                "Boundary repair outcome unknown; independently re-read before retrying"
            ) from None
        if response.status_code not in (200, 204):
            raise RuntimeError(
                f"Boundary repair returned HTTP {response.status_code}; stopped"
            )
        return response


def prepare_repair(records, source):
    """Require the exact reviewed saved record and authoritative daily price."""
    matches = [
        record
        for record in records
        if audit.normalize_saved(record)["record_ref"] == REVIEWED_REF
    ]
    if len(matches) != 1:
        raise ValueError("Reviewed tariff fingerprint changed; no repair permitted")
    record = matches[0]
    normalized = audit.normalize_saved(record)
    if (
        normalized["start"] != REVIEWED_START
        or normalized["end"] != REVIEWED_END
        or Decimal(normalized["pence_per_kwh"]) != REVIEWED_PRICE
    ):
        raise ValueError("Reviewed tariff contents changed; no repair permitted")
    relevant = [
        period
        for period in source
        if period["start_date"]
        <= date.fromisoformat(REVIEWED_START)
        <= (period["end_date"] or date.max)
    ]
    if len(relevant) != 1:
        raise ValueError("Source does not uniquely cover reviewed tariff")
    expected = relevant[0]
    if (
        str(expected["start_date"]) != REVIEWED_START
        or str(expected["end_date"]) != CORRECTED_END
        or abs(Decimal(str(expected["tariff_pence_per_kwh"])) - REVIEWED_PRICE)
        > Decimal("0.00001")
    ):
        raise ValueError("Octopus no longer confirms the reviewed boundary and price")
    return record, {
        "unit": "kWh",
        "startDate": REVIEWED_START,
        "endDate": CORRECTED_END,
        "tariffInCents": float(REVIEWED_PRICE),
    }


def run_repair(tado, session, source, apply=False):
    before = sync.get_tado_tariff_history(tado)
    record, payload = prepare_repair(before, source)
    report = {
        "record_ref": REVIEWED_REF,
        "before_end": REVIEWED_END,
        "payload": payload,
        "status": "review_only_not_executed",
    }
    if not apply:
        return report
    # Re-read immediately before arming the single allowed mutation.
    fresh = sync.get_tado_tariff_history(tado)
    if audit.fingerprint(fresh) != audit.fingerprint(before):
        raise ValueError("Saved tariff history changed during review; stopped")
    request = TadoRequest(
        endpoint=Endpoint.EIQ,
        command="tariffs/" + quote(str(record["id"]), safe=""),
        action=Action.CHANGE,
        payload=payload,
    )
    session.approved_url = tado._http._configure_url(request)
    session.approved_payload = payload
    sync.append_github_step_summary(
        "Reviewed tariff boundary repair",
        [
            ("Record reference", REVIEWED_REF),
            ("Start date", REVIEWED_START),
            ("Old end date", REVIEWED_END),
            ("New end date", CORRECTED_END),
            ("Price p/kWh", REVIEWED_PRICE),
            ("Status", "One PUT about to be attempted; require final read-back"),
        ],
    )
    try:
        tado._http.request(request)
    finally:
        session.approved_url = None
    after = sync.get_tado_tariff_history(tado)
    expected = [
        dict(item, endDate=CORRECTED_END) if item["id"] == record["id"] else item
        for item in before
    ]

    def canonical(items):
        return sorted(items, key=lambda item: str(item["id"]))

    if canonical(after) != canonical(expected):
        raise RuntimeError("Boundary repair read-back differs; stop and investigate")
    report["status"] = "applied_and_independently_verified"
    sync.append_github_step_summary(
        "Tariff repair read-back", [("Status", report["status"])]
    )
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Apply the reviewed PUT")
    args = parser.parse_args()
    required = (
        "TADO_EMAIL",
        "TADO_PASSWORD",
        "OCTOPUS_API_KEY",
        "OCTOPUS_ACCOUNT_NUMBER",
        "OCTOPUS_MPRN",
        "OCTOPUS_GAS_SERIAL",
    )
    if any(not os.environ.get(key) for key in required):
        raise ValueError("Missing configured credentials; no repair attempted")
    session = BoundaryRepairSession()
    tado = sync.tado_login(
        os.environ["TADO_EMAIL"], os.environ["TADO_PASSWORD"], session=session
    )
    source = sync.discover_octopus_tariff_periods(
        os.environ["OCTOPUS_API_KEY"],
        os.environ["OCTOPUS_ACCOUNT_NUMBER"],
        os.environ["OCTOPUS_MPRN"],
        os.environ["OCTOPUS_GAS_SERIAL"],
        since_date=date(2026, 10, 28),
    )
    print(
        json.dumps(run_repair(tado, session, source, apply=args.apply), sort_keys=True)
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("Boundary repair stopped: " + type(error).__name__)
        raise SystemExit(1) from None
