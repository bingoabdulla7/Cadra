"""Evaluation runner for the TCC Desk Compliance Agent.

Reads cases.json, sends each to a running service, validates the response
against the API contract, compares against expected results, and writes
timestamped results to evaluation/results/.

Usage:
    # Start the TCC service first (e.g. uvicorn src.solution:app)
    .venv\Scripts\python evaluation\runner.py [--base-url http://localhost:8000]

If no service is running, the runner can still validate the cases.json schema.
"""

import json
import sys
import os
import argparse
import subprocess
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    requests = None
    print("Warning: requests not installed. Run: pip install requests")


# Outcome precedence order from SOP 9.2 (most restrictive first)
OUTCOME_ORDER = [
    "INVALID_REQUEST",
    "REJECT",
    "HOLD_AND_REFER",
    "PEND",
    "PROCEED_MODIFIED",
    "PROCEED",
]

VALID_OUTCOMES = set(OUTCOME_ORDER)
VALID_REQUEST_TYPES = {
    "NEW_FOLIO_PURCHASE", "ADDITIONAL_PURCHASE", "SIP_REGISTRATION",
    "SIP_INSTALMENT", "REDEMPTION", "CHANGE_OF_BANK", "OTHER",
}
VALID_CHECK_IDS = {
    "KYC_STATUS", "THIRD_PARTY_PAYMENT", "BANK_MANDATE", "COB_COOLING",
    "DISTRIBUTOR_CODE", "MINOR_RULES", "REFERRAL", "FOLIO_IDENTITY",
}
VALID_CHECK_RESULTS = {"PASS", "FAIL", "NOT_APPLICABLE", "UNABLE_TO_VERIFY"}

ALL_REASON_CODES = {
    "KYC_ON_HOLD", "KYC_REJECTED", "KYC_REGISTERED_NEW_AMC",
    "KYC_UNABLE_TO_VERIFY", "TPP_NO_EXCEPTION", "TPP_DOCS_MISSING",
    "BANK_PROOF_REQUIRED", "MINOR_ATTAINED_MAJORITY", "COB_COOLING",
    "HIGH_RISK_CONTACT_COB", "ARN_INVALID_DIRECT", "EUIN_MISSING",
    "REFERRAL_TRIGGER", "STOP_LOCK", "CASH_LIMIT_BREACH",
    "FOLIO_NOT_FOUND", "FOLIO_PAN_MISMATCH",
}


def outcome_is_more_restrictive(a: str, b: str) -> bool:
    """Is outcome a more restrictive than outcome b, per SOP 9.2 order?"""
    return OUTCOME_ORDER.index(a) < OUTCOME_ORDER.index(b)


def validate_contract(response: dict) -> list:
    """Validate response against the API contract. Return list of errors."""
    errors = []

    # Top-level required fields
    required = ["request_id", "request_type", "resolved", "outcome",
                 "reason_codes", "checks", "human_approval_required",
                 "approver_role", "draft_reply", "trace"]
    for field in required:
        if field not in response:
            errors.append(f"Missing field: {field}")

    if not errors:
        # Validate request_type
        if response["request_type"] not in VALID_REQUEST_TYPES:
            errors.append(f"Invalid request_type: {response['request_type']}")

        # Validate outcome
        if response["outcome"] not in VALID_OUTCOMES:
            errors.append(f"Invalid outcome: {response['outcome']}")

        # Validate reason_codes
        if not isinstance(response["reason_codes"], list):
            errors.append("reason_codes must be a list")
        else:
            for rc in response["reason_codes"]:
                if rc not in ALL_REASON_CODES:
                    errors.append(f"Unknown reason code: {rc}")

        # Validate resolved
        resolved = response["resolved"]
        for field in ["folio_no", "amc_code", "first_holder_pan_masked", "txn_id"]:
            if field not in resolved:
                errors.append(f"Missing resolved.{field}")

        # Validate checks
        if not isinstance(response["checks"], list):
            errors.append("checks must be a list")
        else:
            for i, check in enumerate(response["checks"]):
                if "check_id" not in check:
                    errors.append(f"checks[{i}] missing check_id")
                elif check["check_id"] not in VALID_CHECK_IDS:
                    errors.append(f"checks[{i}] unknown check_id: {check['check_id']}")
                if "result" not in check:
                    errors.append(f"checks[{i}] missing result")
                elif check["result"] not in VALID_CHECK_RESULTS:
                    errors.append(f"checks[{i}] invalid result: {check['result']}")
                if "evidence" not in check:
                    errors.append(f"checks[{i}] missing evidence")
                if "sop_clause" not in check:
                    errors.append(f"checks[{i}] missing sop_clause")

        # Validate human_approval_required
        if not isinstance(response["human_approval_required"], bool):
            errors.append("human_approval_required must be boolean")

        # Validate approver_role
        valid_roles = {"CHECKER", "OPS_MANAGER", None}
        if response["approver_role"] not in valid_roles:
            errors.append(f"Invalid approver_role: {response['approver_role']}")

        # Validate draft_reply
        for field in ["to", "subject", "body"]:
            if field not in response.get("draft_reply", {}):
                errors.append(f"Missing draft_reply.{field}")

        # Validate trace
        trace = response.get("trace", {})
        if "model_calls" not in trace:
            errors.append("Missing trace.model_calls")
        if "tools_called" not in trace:
            errors.append("Missing trace.tools_called")
        if "documents_used" not in trace:
            errors.append("Missing trace.documents_used")
        if "cost_usd" not in trace:
            errors.append("Missing trace.cost_usd")

    return errors


def classify_error(actual_outcome: str, expected_outcome: str) -> str:
    """Classify an error as unsafe, over-cautious, or different."""
    if actual_outcome == expected_outcome:
        return "correct"
    
    if outcome_is_more_restrictive(actual_outcome, expected_outcome):
        return "over_cautious"
    elif outcome_is_more_restrictive(expected_outcome, actual_outcome):
        return "unsafe"
    else:
        # Shouldn't happen since one must be more restrictive unless equal
        return "different"


def run_case(base_url: str, case: dict) -> dict:
    """Send a single case to the service and return the result."""
    result = {
        "case_id": case["case_id"],
        "expected": case.get("expected", {}),
        "needs_confirmation": case.get("needs_confirmation"),
        "error": None,
        "contract_errors": [],
        "response": None,
        "outcome_match": None,
        "error_type": None,
    }

    # Build a minimal request payload from the case
    # If the case has an 'input' field, use it directly
    # Otherwise build one from the description
    if "input" in case:
        payload = case["input"]
    else:
        # Generic fallback - cases without input can't be run against the API
        result["error"] = "No input payload in case definition"
        return result

    try:
        url = f"{base_url}/requests"
        resp = requests.post(url, json=payload, timeout=30)
        result["http_status"] = resp.status_code

        if resp.status_code != 200:
            result["error"] = f"HTTP {resp.status_code}: {resp.text[:200]}"
            return result

        response_data = resp.json()
        result["response"] = response_data

        # Validate contract
        contract_errors = validate_contract(response_data)
        result["contract_errors"] = contract_errors

        # Compare outcomes
        expected = case.get("expected", {})
        expected_outcome = expected.get("outcome")
        actual_outcome = response_data.get("outcome")

        if expected_outcome and actual_outcome:
            result["outcome_match"] = actual_outcome == expected_outcome
            if not result["outcome_match"]:
                result["error_type"] = classify_error(
                    actual_outcome, expected_outcome
                )
                result["expected_outcome"] = expected_outcome
                result["actual_outcome"] = actual_outcome

        # Compare reason codes
        expected_reasons = set(expected.get("reason_codes", []))
        actual_reasons = set(response_data.get("reason_codes", []))
        if expected_reasons or actual_reasons:
            missing_reasons = expected_reasons - actual_reasons
            extra_reasons = actual_reasons - expected_reasons
            result["reason_code_issues"] = {}
            if missing_reasons:
                result["reason_code_issues"]["missing"] = list(missing_reasons)
            if extra_reasons:
                result["reason_code_issues"]["extra"] = list(extra_reasons)

    except requests.exceptions.ConnectionError:
        result["error"] = f"Cannot connect to {base_url}"
    except requests.exceptions.Timeout:
        result["error"] = "Request timed out"
    except Exception as e:
        result["error"] = f"Exception: {e}"

    return result


def signal_done():
    """Send a keepalive signal but do nothing harmful."""
    pass


def main():
    parser = argparse.ArgumentParser(description="TCC Evaluation Runner")
    parser.add_argument("--base-url", default="http://localhost:8000",
                        help="Base URL of the TCC service")
    args = parser.parse_args()

    cases_path = Path(__file__).parent / "cases.json"
    results_dir = Path(__file__).parent / "results"
    results_dir.mkdir(parents=True, exist_ok=True)

    with open(cases_path, encoding="utf-8") as f:
        cases = json.load(f)

    # Validate cases.json schema
    for i, case in enumerate(cases):
        if "case_id" not in case:
            print(f"ERROR: case[{i}] missing case_id")
            sys.exit(1)
        if "description" not in case:
            print(f"ERROR: {case['case_id']} missing description")

    print(f"Loaded {len(cases)} cases from cases.json")
    print(f"Target service: {args.base_url}/health")

    # Check if service is alive
    service_ok = False
    if requests:
        try:
            h = requests.get(f"{args.base_url}/health", timeout=5)
            if h.status_code == 200 and h.json().get("status") == "ok":
                service_ok = True
                print("Service is running and healthy.")
            else:
                print(f"Health check returned: {h.status_code} {h.text[:100]}")
        except Exception as e:
            print(f"Cannot reach service: {e}")
    else:
        print("Requests library not available. Will validate cases.json only.")

    # Run each case that has an input
    results = []
    stats = {
        "total": len(cases),
        "ran": 0,
        "skipped_no_input": 0,
        "http_errors": 0,
        "contract_errors": 0,
        "correct_outcomes": 0,
        "unsafe_errors": 0,
        "over_cautious_errors": 0,
    }

    for case in cases:
        if "input" not in case or not service_ok or not requests:
            stats["skipped_no_input"] += 1
            results.append({
                "case_id": case["case_id"],
                "skipped": True,
                "reason": "No input payload" if "input" not in case else "Service not available",
            })
            continue

        result = run_case(args.base_url, case)
        results.append(result)
        stats["ran"] += 1

        if result.get("error"):
            stats["http_errors"] += 1
        if result.get("contract_errors"):
            stats["contract_errors"] += len(result["contract_errors"])

        outcome_match = result.get("outcome_match")
        if outcome_match is True:
            stats["correct_outcomes"] += 1
        elif outcome_match is False:
            error_type = result.get("error_type")
            if error_type == "unsafe":
                stats["unsafe_errors"] += 1
            elif error_type == "over_cautious":
                stats["over_cautious_errors"] += 1

    # Build summary
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    run_result = {
        "run_timestamp_utc": timestamp,
        "service_url": args.base_url,
        "statistics": stats,
        "results": results,
    }

    # Write results
    results_file = results_dir / f"{timestamp}.json"
    with open(results_file, "w", encoding="utf-8") as f:
        json.dump(run_result, f, indent=2, default=str)
    print(f"\nResults written to {results_file}")

    # Print summary
    print(f"\n=== Summary ===")
    print(f"Total cases:      {stats['total']}")
    print(f"Ran:              {stats['ran']}")
    print(f"Skipped (no input): {stats['skipped_no_input']}")
    print(f"HTTP errors:      {stats['http_errors']}")
    print(f"Contract errors:  {stats['contract_errors']}")
    print(f"Correct outcomes: {stats['correct_outcomes']}")
    print(f"Unsafe errors:    {stats['unsafe_errors']}")
    print(f"Over-cautious:    {stats['over_cautious_errors']}")

    if stats["ran"] > 0:
        accuracy = stats["correct_outcomes"] / stats["ran"] * 100
        print(f"First accuracy:   {accuracy:.1f}%")


if __name__ == "__main__":
    main()