"""src/outcome.py — Final decision from checks."""

from __future__ import annotations
from datetime import datetime, timedelta

def decide(result: dict, req: dict) -> dict:
    checks = result.get("checks", [])
    request_type = result.get("request_type", "unknown")
    hints = []
    codes = []
    has_unable = False

    for c in checks:
        r = c.get("result", "")
        if r in ("FAIL",):
            h = c.get("hint")
            if h:
                hints.append(h)
            codes.extend(c.get("codes", []))
        elif r == "UNABLE_TO_VERIFY":
            has_unable = True
            h = c.get("hint", "PEND")
            if h:
                hints.append(h)
            codes.extend(c.get("codes", []))

    codes = list(dict.fromkeys(codes))

    hint_order = ["INVALID_REQUEST", "REJECT", "HOLD_AND_REFER", "PEND", "PROCEED_MODIFIED", "PROCEED"]
    final_hint = "PROCEED"
    for h in hint_order:
        if h in hints:
            final_hint = h
            break

    if has_unable and final_hint == "PROCEED":
        final_hint = "PEND"

    if request_type == "OTHER":
        final_hint = "PEND"

    outcome_map = {
        "INVALID_REQUEST": "INVALID_REQUEST",
        "REJECT": "REJECT",
        "HOLD_AND_REFER": "HOLD_AND_REFER",
        "PEND": "PEND",
        "PROCEED_MODIFIED": "PROCEED_MODIFIED",
        "PROCEED": "PROCEED",
    }
    outcome = outcome_map.get(final_hint, "PEND")

    approver_map = {
        "PROCEED": None,
        "PEND": None,
        "INVALID_REQUEST": None,
        "PROCEED_MODIFIED": "CHECKER",
        "REJECT": "CHECKER",
        "HOLD_AND_REFER": "OPS_MANAGER",
    }
    approver_role = approver_map.get(outcome)

    human_approval_required = outcome in ("PROCEED_MODIFIED", "REJECT", "HOLD_AND_REFER")

    # Decision basis: list of dicts with check_id, sop_clause, evidence
    decision_basis = [
        {
            "check_id": c["check_id"],
            "sop_clause": c.get("sop_clause", ""),
            "evidence": c.get("evidence", []),
        }
        for c in checks
        if c.get("result") in ("FAIL", "UNABLE_TO_VERIFY")
    ]

    # Draft reply
    draft_reply = _build_reply(outcome, request_type, req, checks, codes)

    if outcome == "PROCEED":
        for c in checks:
            assert c.get("result") in ("PASS", "NOT_APPLICABLE", None), f"Check {c['check_id']} is {c.get('result')}"

    return {
        "outcome": outcome,
        "reason_codes": codes,
        "human_approval_required": human_approval_required,
        "approver_role": approver_role,
        "draft_reply": draft_reply,
        "draft_status": "draft",
        "decision_basis": decision_basis,
    }


def _fmt_inr(n):
    """Indian digit grouping."""
    s = f"{int(n):,}"
    if len(s) > 3:
        last3 = s[-3:]
        rest = s[:-3].replace(",", "")
        if rest:
            return f"{rest},{last3}"
    return s


def _build_reply(outcome, request_type, req, checks, codes):
    received_at = req.get("_received_at", "")
    sender_name = ""
    frm = req.get("from", "")
    if "<" in frm:
        sender_name = frm.split("<")[0].strip().strip('"\'')
    elif frm:
        sender_name = frm.split("@")[0].replace(".", " ").title()
    if not sender_name:
        sender_name = "Investor"
    to = req.get("from", "operations@tcc.com")
    subject = f"Re: {req.get('subject', '')}"
    body = ""

    if outcome in ("INVALID_REQUEST", "PEND", "HOLD_AND_REFER"):
        if outcome == "INVALID_REQUEST":
            body = f"Dear {sender_name},\n\nThank you for your request. We have received it but are unable to process it at this time. Our team will review your request and get back to you.\n\nRegards,\nTCC Team"
        elif outcome == "PEND" and request_type == "OTHER":
            body = f"Dear {sender_name},\n\nThank you for your request. Your request is currently being reviewed. An update will be provided shortly.\n\nRegards,\nTCC Team"
        elif outcome == "PEND":
            tpp_missing = []
            bank_proof = False
            kyc_unable = False
            kyc_reg_new = False
            minor_majority = False
            for c in checks:
                if "TPP_DOCS_MISSING" in c.get("codes", []):
                    for ev in c.get("evidence", []):
                        if ev.get("value", "").startswith("Missing:"):
                            tpp_missing.append(ev["value"])
                if "BANK_PROOF_REQUIRED" in c.get("codes", []):
                    bank_proof = True
                if "KYC_UNABLE_TO_VERIFY" in c.get("codes", []):
                    kyc_unable = True
                if "KYC_REGISTERED_NEW_AMC" in c.get("codes", []):
                    kyc_reg_new = True
                if "MINOR_ATTAINED_MAJORITY" in c.get("codes", []):
                    minor_majority = True
            parts = [f"Dear {sender_name},"]
            if tpp_missing:
                parts.append("\nWe require the following documents to proceed:")
                for m in tpp_missing:
                    parts.append(f"- {m}")
            elif bank_proof:
                parts.append("\nPlease provide a bank statement or transfer advice showing the debit (or the change-of-bank proofs).")
            elif kyc_unable:
                parts.append("\nYour request is on hold and will be re-checked in the next daily cycle.")
            elif kyc_reg_new:
                parts.append("\nPlease arrange for fresh KYC compliance.")
            elif minor_majority:
                parts.append("\nPlease provide the change-of-status form, the investor's own KYC, and new bank details.")
            else:
                parts.append("\nWe require the following documents to proceed:\n\nPlease submit these documents at your earliest convenience.")
            parts.append("\n\nRegards,\nTCC Team")
            body = "\n".join(parts)
        elif outcome == "HOLD_AND_REFER":
            cooling_end = None
            for c in checks:
                if c.get("check_id") == "COB_COOLING" and c.get("result") == "FAIL":
                    for ev in c.get("evidence", []):
                        if "COB" in ev.get("value", ""):
                            cob = ev["value"].split()[-1]
                            try:
                                cd = datetime.strptime(cob, "%Y-%m-%d").date()
                                cooling_end = (cd + timedelta(days=10)).strftime("%d %B %Y")
                            except:
                                pass
            if not cooling_end:
                cooling_end = _calc_2wd(received_at)
            body = f"Dear {sender_name},\n\nThank you for your request. It is currently under processing. We expect to provide an update by {cooling_end}.\n\nRegards,\nTCC Team"
    elif outcome == "REJECT":
        reason = ""
        if "CASH_LIMIT_BREACH" in codes:
            reason = "the transaction exceeds the permissible cash limit"
        elif "KYC_ON_HOLD" in codes or "KYC_REJECTED" in codes:
            reason = "your KYC status does not meet the requirements"
        elif "TPP_NO_EXCEPTION" in codes:
            reason = "the payment source is not acceptable"
        body = f"Dear {sender_name},\n\nThank you for your request. Unfortunately, we are unable to process it as {reason}. The amount will be refunded to the source account.\n\nRegards,\nTCC Team"
    elif outcome == "PROCEED_MODIFIED":
        modifications = []
        if "ARN_INVALID_DIRECT" in codes:
            modifications.append("Your application has been processed on a direct basis as the ARN provided was not valid")
        if "EUIN_MISSING" in codes:
            modifications.append("Your EUIN was not provided")
        mod_text = "; ".join(modifications)
        body = f"Dear {sender_name},\n\nThank you for your request. {mod_text}.\n\nRegards,\nTCC Team"
    else:  # PROCEED
        body = f"Dear {sender_name},\n\nThank you for your request. It has been checked and released for processing.\n\nRegards,\nTCC Team"

    return {"to": to, "subject": subject, "body": body}


def _calc_2wd(received_at):
    """Simple 2 working day estimate."""
    try:
        dt = datetime.strptime(received_at[:10], "%Y-%m-%d")
    except:
        return "within 2 working days"
    count = 0
    while count < 2:
        dt += timedelta(days=1)
        if dt.weekday() < 5:
            count += 1
    return dt.strftime("%d %B %Y")