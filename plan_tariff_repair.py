"""Build a review-only, ID-preserving repair plan from redacted audit evidence."""

import argparse
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import audit_tariffs as audit


def bounds(period):
    return date.fromisoformat(period["start"]), (
        date.fromisoformat(period["end"]) if period["end"] else date.max
    )


def overlaps(left, right):
    a, b = bounds(left)
    c, d = bounds(right)
    return a <= d and c <= b


def payload(period):
    price = Decimal(period["pence_per_kwh"])
    numeric = float(price)
    if Decimal(str(numeric)) != price:
        raise ValueError("Price cannot be represented exactly in the JSON payload")
    result = {"unit": "kWh", "startDate": period["start"], "tariffInCents": numeric}
    if period["end"] is not None:
        result["endDate"] = period["end"]
    return result


def build_plan(snapshot, scenario):
    confirmation = scenario.get("payment_method_confirmation")
    if (
        not isinstance(confirmation, dict)
        or confirmation.get("confirmed_by") != "user"
        or confirmation.get("method") not in ("DIRECT_DEBIT", "NON_DIRECT_DEBIT")
    ):
        raise ValueError(
            "This plan requires an explicitly confirmed payment-method scenario"
        )
    if scenario["saved_snapshot_sha256"] != snapshot["saved_snapshot_sha256"]:
        raise ValueError("Saved snapshot fingerprints differ")
    canonical_saved = sorted(
        snapshot["saved_periods"], key=lambda x: (x["start"], x["record_ref"])
    )
    if audit.fingerprint(canonical_saved) != snapshot["saved_snapshot_sha256"]:
        raise ValueError("Saved snapshot contents do not match their fingerprint")
    if (
        audit.fingerprint(scenario["source_periods"])
        != scenario["source_snapshot_sha256"]
    ):
        raise ValueError("Source scenario contents do not match their fingerprint")
    old = sorted(scenario["replace_candidates"], key=lambda x: x["start"])
    target = sorted(scenario["create_candidates"], key=lambda x: x["start"])
    # Monotone matching preserves chronological record identity. Every update
    # must retain at least one old covered day; unmatched target fragments are
    # new records. Dynamic programming maximizes retained overlap.
    memo = {}

    def match(i, j):
        if i == len(old):
            return (0, [])
        if len(target) - j < len(old) - i:
            return None
        key = (i, j)
        if key in memo:
            return memo[key]
        best = match(i, j + 1) if j < len(target) else None
        if j < len(target) and overlaps(old[i], target[j]):
            tail = match(i + 1, j + 1)
            if tail is not None:
                a, b = bounds(old[i])
                c, d = bounds(target[j])
                score = (min(b, d) - max(a, c)).days + 1
                candidate = (tail[0] + score, [(i, j)] + tail[1])
                if best is None or candidate[0] > best[0]:
                    best = candidate
        memo[key] = best
        return best

    matched = match(0, 0)
    if matched is None:
        raise ValueError("Cannot retain all record IDs without an ambiguous remapping")
    assignments = dict(matched[1])
    pending = set(assignments)
    current = {i: old[i] for i in assignments}
    update_order = []
    while pending:
        ready = [
            i
            for i in pending
            if not any(
                i != other and overlaps(target[assignments[i]], period)
                for other, period in current.items()
            )
        ]
        if not ready:
            raise ValueError("Repair would require a temporary overlapping state")
        for i in sorted(ready, reverse=True):
            current[i] = target[assignments[i]]
            pending.remove(i)
            update_order.append(i)
    operations = [
        {
            "method": "PUT",
            "record_ref": old[i]["record_ref"],
            "before": old[i],
            "path_template": "https://energy-insights.tado.com/api/homes/{home_id}/tariffs/{tariff_id}",
            "payload": payload(target[assignments[i]]),
        }
        for i in update_order
    ]
    assigned_targets = set(assignments.values())
    operations.extend(
        {
            "method": "POST",
            "path_template": "https://energy-insights.tado.com/api/homes/{home_id}/tariffs",
            "payload": payload(period),
        }
        for j, period in enumerate(target)
        if j not in assigned_targets
    )
    # Simulate every step against the full saved state, including kept records.
    state = {record["record_ref"]: record for record in snapshot["saved_periods"]}
    for index, operation in enumerate(operations):
        ref = operation.get("record_ref", f"created-{index}")
        value = operation["payload"]
        proposed = {
            "start": value["startDate"],
            "end": value.get("endDate"),
            "pence_per_kwh": str(value["tariffInCents"]),
        }
        if any(
            other != ref and overlaps(proposed, period)
            for other, period in state.items()
        ):
            raise ValueError("Simulated operation overlaps another saved record")
        state[ref] = proposed
    simulated = [
        {
            "id": ref,
            "startDate": p["start"],
            "endDate": p["end"],
            "tariffInCents": p["pence_per_kwh"],
            "unit": "kWh",
        }
        for ref, p in state.items()
    ]
    expected = [
        {
            "start_date": date.fromisoformat(p["start"]),
            "end_date": date.fromisoformat(p["end"]) if p["end"] else None,
            "tariff_pence_per_kwh": p["pence_per_kwh"],
        }
        for p in scenario["source_periods"]
    ]
    checked = audit.build_audit(
        simulated, expected, today=date.fromisoformat(scenario["audit_date"])
    )
    if checked["replace_candidates"] or checked["create_candidates"]:
        raise ValueError("Simulated repair does not completely match source coverage")
    plan = {
        "status": "review_only_not_executed",
        "payment_method_confirmation": confirmation,
        "saved_snapshot_sha256": snapshot["saved_snapshot_sha256"],
        "source_snapshot_sha256": scenario["source_snapshot_sha256"],
        "counts": {
            "keep": len(snapshot["saved_periods"]) - len(old),
            "update": len(old),
            "create": len(target) - len(old),
            "delete": 0,
        },
        "simulation": "Every intermediate state is overlap-free; final daily prices and coverage match the selected source snapshot.",
        "operations": operations,
    }
    plan["plan_sha256"] = audit.fingerprint(plan)
    return plan


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    plan = build_plan(
        json.loads(Path(args.snapshot).read_text()),
        json.loads(Path(args.scenario).read_text()),
    )
    Path(args.output).write_text(json.dumps(plan, indent=2))
    print(json.dumps({"counts": plan["counts"], "plan_sha256": plan["plan_sha256"]}))
