"""src/checks.py — SOP evaluation checks."""

from __future__ import annotations
import re
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))

EVIDENCE_SOURCES = {
    "folio_master": "folio_master.csv",
    "kra_kyc_snapshot": "kra_kyc_snapshot.csv",
    "bank_mandates": "bank_mandates.csv",
    "transactions": "transactions.csv",
    "distributor_master": "distributor_master.csv",
}

_INTAKE_TO_TYPE = {
    "new folio": "NEW_FOLIO_PURCHASE",
    "additional purchase": "ADDITIONAL_PURCHASE",
    "SIP registration": "SIP_REGISTRATION",
    "SIP instalment": "SIP_INSTALMENT",
    "redemption": "REDEMPTION",
    "change of bank": "CHANGE_OF_BANK",
    "other": "OTHER",
}

_TXN_TYPE_MAP = {
    "REDEMPTION": "RED",
    "ADDITIONAL_PURCHASE": "ADD",
    "NEW_FOLIO_PURCHASE": "NEW",
    "SIP_REGISTRATION": "SIP_REG",
    "SIP_INSTALMENT": "SIP",
}

_PURCHASE_LIKE = {"NEW_FOLIO_PURCHASE", "ADDITIONAL_PURCHASE", "SIP_REGISTRATION", "SIP_INSTALMENT"}


def evaluate(store, req, intake):
    guarded = intake.get("guarded_facts", {})
    folios = guarded.get("folios", [])
    amounts = guarded.get("amounts", [])
    utr = guarded.get("utr")
    arn = guarded.get("arn")
    euin = guarded.get("euin")
    last4_accounts = guarded.get("last4_account", [])
    payment_mode = guarded.get("payment_mode")
    fund_house = guarded.get("fund_house")
    request_type_guess = guarded.get("request_type", "unknown")

    docs = intake.get("docs", [])
    classifications = intake.get("classifications", [])
    warnings = intake.get("warnings", [])

    received_at = req["received_at"]
    email_text = req["subject"] + "\n" + req["body"]

    request_type = _INTAKE_TO_TYPE.get(request_type_guess, "OTHER")

    policy, policy_docs = store.select_policy(received_at, fund_house) if fund_house else (None, [])
    if policy is None:
        policy, policy_docs = store.select_policy(received_at, fund_house or "NMF")
    if policy is None:
        return _unable_policy(intake)
    sop_ver = policy.get("version", "4.1")
    sop_base = f"TCC-SOP v{sop_ver}"
    addendum = policy.get("addendum")

    # ── Identity ──
    identity_check, folio_row, lname = _identity_check(store, folios, fund_house, request_type_guess, email_text, intake)
    if identity_check is not None:
        ev = _make_evidence(lname, str(folios) if folios else "", identity_check.get("codes", [""])[0])
        identity_check["evidence"] = [ev]
        checks = [identity_check]
        for cid in ("KYC_STATUS", "THIRD_PARTY_PAYMENT", "BANK_MANDATE", "COB_COOLING",
                     "DISTRIBUTOR_CODE", "MINOR_RULES", "REFERRAL"):
            checks.append(_na_check(cid, "Identity not resolved"))
        return {
            "request_type": request_type,
            "resolved": {"folio_no": None, "amc_code": None,
                         "first_holder_pan_masked": None, "txn_id": None},
            "checks": checks,
            "tools_called": [],
            "policy_docs": policy_docs,
            "duplicate_txn_ids": [],
            "flags": [],
        }

    folio_no = folio_row["folio_no"]
    amc_code = folio_row["amc_code"].strip()
    first_holder_pan_masked = folio_row["first_holder_pan_masked"].strip()
    pan = first_holder_pan_masked

    # ── Transaction matching ──
    txns, txns_src = store.lookup_transactions(folio_no)
    pending_txns = [t for t in txns if t.get("status", "").strip() == "PENDING_TCC"]
    matched_txn = None
    duplicate_txn_ids = []

    purchase_types = {"NEW", "ADD", "SIP_REG", "SIP"}
    if request_type in _PURCHASE_LIKE:
        possible = [t for t in pending_txns if t.get("txn_type", "").strip() in purchase_types]
    elif request_type == "REDEMPTION":
        possible = [t for t in pending_txns if t.get("txn_type", "").strip() == "RED"]
    else:
        possible = pending_txns  # OTHER, CHANGE_OF_BANK — any match

    # Match by UTR then amount against the candidate pool
    if utr:
        for t in possible:
            if t.get("utr", "").strip() == utr:
                matched_txn = t
                break
    if matched_txn is None and amounts:
        for a in amounts:
            for t in possible:
                if abs(t["_amount"] - a) < 0.001:
                    matched_txn = t
                    break
            if matched_txn:
                break

    # After a match, always set request_type from the matched row's txn_type
    if matched_txn:
        txn_t = matched_txn.get("txn_type", "").strip()
        rev_map = {"RED": "REDEMPTION", "ADD": "ADDITIONAL_PURCHASE",
                    "NEW": "NEW_FOLIO_PURCHASE", "SIP_REG": "SIP_REGISTRATION",
                    "SIP": "SIP_INSTALMENT"}
        request_type = rev_map.get(txn_t, request_type)

    # Deduplicate twins — string comparison, no int()
    if matched_txn:
        twin_key = (matched_txn.get("utr", "").strip(), str(matched_txn["_amount"]),
                    matched_txn.get("remitter_account_masked", "").strip())
        twins = [matched_txn]
        for t in pending_txns:
            if t["txn_id"] == matched_txn["txn_id"]:
                continue
            tk = (t.get("utr", "").strip(), str(t["_amount"]), t.get("remitter_account_masked", "").strip())
            if tk == twin_key:
                twins.append(t)
        twins.sort(key=lambda x: x["txn_id"])
        matched_txn = twins[0]
        duplicate_txn_ids = sorted(set(t["txn_id"] for t in twins[1:]))

    txn_id = matched_txn["txn_id"] if matched_txn else None
    txn_amount = matched_txn["_amount"] if matched_txn else (amounts[0] if amounts else 0)
    txn_date = matched_txn["_txn_date"] if matched_txn else ""

    checks = []
    result = {
        "request_type": request_type,
        "resolved": {"folio_no": folio_no, "amc_code": amc_code,
                     "first_holder_pan_masked": first_holder_pan_masked, "txn_id": txn_id},
        "checks": checks,
        "tools_called": [],
        "policy_docs": policy_docs,
        "duplicate_txn_ids": duplicate_txn_ids,
        "flags": [],
    }

    # 1. FOLIO_IDENTITY
    checks.append({"check_id": "FOLIO_IDENTITY", "result": "PASS",
                    "evidence": [_make_evidence("folio_master", folio_no, f"Folio {folio_no} resolved")],
                    "sop_clause": f"{sop_base} 9.4", "codes": [], "hint": None})

    # 2. KYC_STATUS
    _kyc_check(store, pan, request_type, fund_house, received_at, sop_base, checks, email_text)

    # 3. THIRD_PARTY_PAYMENT
    _third_party_check(store, folio_no, folio_row, matched_txn, request_type, payment_mode,
                       email_text, docs, classifications, sop_base, checks, txn_amount)

    # 4. BANK_MANDATE
    _bank_mandate_check(store, folio_no, matched_txn, request_type, last4_accounts,
                        payment_mode, docs, classifications, email_text, fund_house,
                        sop_base, addendum, checks)

    # 5. COB_COOLING
    _cob_cooling_check(store, folio_no, request_type, txn_date, sop_base, checks)

    # 6. DISTRIBUTOR_CODE
    _distributor_check(store, matched_txn, request_type, arn, euin, fund_house, txn_date,
                       sop_base, checks)

    # 7. MINOR_RULES
    _minor_check(store, folio_row, matched_txn, sop_base, checks)

    # 8. REFERRAL
    _referral_check(store, folio_no, pan, fund_house, request_type, txn_date, txn_amount,
                    received_at, matched_txn, addendum, sop_base, checks)

    return result


def _unable_policy(intake):
    return {
        "request_type": intake.get("guarded_facts", {}).get("request_type", "unknown"),
        "resolved": {},
        "checks": [{"check_id": "POLICY", "result": "UNABLE_TO_VERIFY",
                     "evidence": [], "sop_clause": "TCC-SOP v4.1 1.0",
                     "codes": [], "hint": "PEND"}],
        "tools_called": [],
        "policy_docs": [],
        "duplicate_txn_ids": [],
        "flags": [],
    }


def _na_check(cid, reason=""):
    return {"check_id": cid, "result": "NOT_APPLICABLE",
            "evidence": [{"source": "", "ref": "", "value": reason or "Not applicable"}],
            "sop_clause": "", "codes": [], "hint": None}


def _make_evidence(source_key, ref, value):
    name = EVIDENCE_SOURCES.get(source_key, source_key)
    return {"source": name, "ref": str(ref), "value": str(value)}


# ── Helpers ──────────────────────────────────────────────────────────────────

def _get_holders(row):
    names = []
    if row.get("first_holder_name", "").strip():
        names.append(row["first_holder_name"].strip())
    if row.get("second_holder_name", "").strip():
        names.append(row["second_holder_name"].strip())
    if row.get("guardian_name", "").strip():
        names.append(row["guardian_name"].strip())
    return names


def _single_name_match(name_a, name_b):
    """Order- and initial-tolerant match: every word of the shorter name equals
    a word of the longer or is its initial; at least one full word shared."""
    a_parts = [w for w in name_a.strip().lower().split() if w]
    b_parts = [w for w in name_b.strip().lower().split() if w]
    if not a_parts or not b_parts:
        return False
    shorter = a_parts if len(a_parts) <= len(b_parts) else b_parts
    longer = b_parts if len(a_parts) <= len(b_parts) else a_parts
    full_shared = 0
    for w in shorter:
        w = w.replace(".", "")
        if w in longer:
            if len(w) > 1:
                full_shared += 1
            continue
        if len(w) == 1 and any(lo[0] == w for lo in longer):
            continue
        return False
    return full_shared >= 1


def _name_matches(investor, folio_holders):
    for h in folio_holders:
        if _single_name_match(investor, h):
            return True
    return False


# ── Identity ─────────────────────────────────────────────────────────────────

def _identity_check(store, folios, fund_house, request_type_guess, email_text, intake=None):
    if not folios:
        return _id_fail_dict("FOLIO_NOT_FOUND"), None, "folio_master"
    # More than one distinct folio → PAN mismatch even same fund house
    if len(folios) > 1:
        distinct_folios = list(dict.fromkeys(folios))
        if len(distinct_folios) > 1:
            return _id_fail_dict("FOLIO_PAN_MISMATCH"), None, "folio_master"
    folio = folios[0]
    folio_row, lname = store.lookup_folio(folio)
    if folio_row is None:
        return _id_fail_dict("FOLIO_NOT_FOUND"), None, lname
    if fund_house and fund_house != folio_row["amc_code"].strip():
        if request_type_guess != "new folio":
            return _id_fail_dict("FOLIO_PAN_MISMATCH"), None, lname
    if intake:
        investor_name = intake.get("investor_name")
        if investor_name:
            holders = _get_holders(folio_row)
            if not _name_matches(investor_name, holders):
                ev = _make_evidence("folio_master", folio_row.get("folio_no", str(folios)), f"investor name '{investor_name}' not in {holders}")
                return {"check_id": "FOLIO_IDENTITY", "result": "FAIL",
                        "evidence": [ev],
                        "sop_clause": "TCC-SOP v4.1 9.4",
                        "codes": ["FOLIO_PAN_MISMATCH"], "hint": "INVALID_REQUEST"}, None, lname
    return None, folio_row, lname


def _id_fail_dict(code):
    return {"check_id": "FOLIO_IDENTITY", "result": "FAIL",
            "evidence": [],
            "sop_clause": "TCC-SOP v4.1 9.4",
            "codes": [code], "hint": "INVALID_REQUEST"}


# ── KYC ──────────────────────────────────────────────────────────────────────

def _kyc_check(store, pan, request_type, fund_house, received_at, sop_base, checks, email_text=""):
    kra_row, lname = store.lookup_kra(pan)
    source = "kra_kyc_snapshot"
    if kra_row is None:
        checks.append({"check_id": "KYC_STATUS", "result": "UNABLE_TO_VERIFY",
                        "evidence": [_make_evidence(source, pan, "No KYC record")],
                        "sop_clause": f"{sop_base} 4.5",
                        "codes": ["KYC_UNABLE_TO_VERIFY"], "hint": "PEND"})
        return

    last_checked = kra_row.get("_last_checked_dt", "")
    try:
        dt_recv = datetime.fromisoformat(received_at)
        # Parse _last_checked_dt as naive string, treat as IST
        lc_dt_naive = datetime.strptime(last_checked[:19], "%Y-%m-%d %H:%M:%S") if len(last_checked) >= 19 else datetime.strptime(last_checked[:16], "%Y-%m-%d %H:%M")
        lc_dt = lc_dt_naive.replace(tzinfo=IST)
        if dt_recv - lc_dt > timedelta(hours=24):
            checks.append({"check_id": "KYC_STATUS", "result": "UNABLE_TO_VERIFY",
                            "evidence": [_make_evidence(source, pan, f"Stale check {last_checked}")],
                            "sop_clause": f"{sop_base} 4.5",
                            "codes": ["KYC_UNABLE_TO_VERIFY"], "hint": "PEND"})
            return
    except:
        checks.append({"check_id": "KYC_STATUS", "result": "UNABLE_TO_VERIFY",
                        "evidence": [_make_evidence(source, pan, f"Bad date: {last_checked}")],
                        "sop_clause": f"{sop_base} 4.5",
                        "codes": ["KYC_UNABLE_TO_VERIFY"], "hint": "PEND"})
        return

    status = kra_row.get("_derived_kra_status", "UNKNOWN")
    if status == "VALIDATED":
        checks.append({"check_id": "KYC_STATUS", "result": "PASS",
                        "evidence": [_make_evidence(source, pan, f"Status: {status}")],
                        "sop_clause": f"{sop_base} 4.2",
                        "codes": [], "hint": None})
    elif status == "REGISTERED":
        folios_for_pan, _ = store.lookup_folio_by_pan(pan)
        if request_type == "NEW_FOLIO_PURCHASE":
            # Find target fund house from email text
            fh_lower = email_text.lower()
            target_fh = None
            if "narmada" in fh_lower or "nmf" in fh_lower:
                target_fh = "NMF"
            elif "tungabhadra" in fh_lower or "tmf" in fh_lower:
                target_fh = "TMF"
            if not target_fh:
                target_fh = fund_house
            if target_fh:
                existing = [f for f in folios_for_pan if store.lookup_folio(f)[0] and store.lookup_folio(f)[0].get("amc_code", "").strip() == target_fh]
                if not existing:
                    checks.append({"check_id": "KYC_STATUS", "result": "FAIL",
                                    "evidence": [_make_evidence(source, pan, f"REGISTERED, no folio at {target_fh}")],
                                    "sop_clause": f"{sop_base} 4.3",
                                    "codes": ["KYC_REGISTERED_NEW_AMC"], "hint": "PEND"})
                    return
        checks.append({"check_id": "KYC_STATUS", "result": "PASS",
                        "evidence": [_make_evidence(source, pan, f"Status: {status}")],
                        "sop_clause": f"{sop_base} 4.2",
                        "codes": [], "hint": None})
    elif status == "ON_HOLD":
        hint = "REJECT" if request_type in ("NEW_FOLIO_PURCHASE", "ADDITIONAL_PURCHASE") else "HOLD_AND_REFER"
        checks.append({"check_id": "KYC_STATUS", "result": "FAIL",
                        "evidence": [_make_evidence(source, pan, f"Status: {status}")],
                        "sop_clause": f"{sop_base} 4.4",
                        "codes": ["KYC_ON_HOLD"], "hint": hint})
    elif status == "REJECTED":
        hint = "REJECT" if request_type in ("NEW_FOLIO_PURCHASE", "ADDITIONAL_PURCHASE") else "HOLD_AND_REFER"
        checks.append({"check_id": "KYC_STATUS", "result": "FAIL",
                        "evidence": [_make_evidence(source, pan, f"Status: {status}")],
                        "sop_clause": f"{sop_base} 4.4",
                        "codes": ["KYC_REJECTED"], "hint": hint})
    else:
        checks.append({"check_id": "KYC_STATUS", "result": "UNABLE_TO_VERIFY",
                        "evidence": [_make_evidence(source, pan, f"Status: {status}")],
                        "sop_clause": f"{sop_base} 4.5",
                        "codes": ["KYC_UNABLE_TO_VERIFY"], "hint": "PEND"})


# ── Third-party payment ──────────────────────────────────────────────────────

def _third_party_check(store, folio_no, folio_row, matched_txn, request_type,
                       payment_mode, email_text, docs, classifications, sop_base,
                       checks, txn_amount):
    if request_type not in _PURCHASE_LIKE:
        checks.append(_na_check("THIRD_PARTY_PAYMENT", "Not a purchase or SIP"))
        return
    if payment_mode and payment_mode.strip().upper() == "CASH":
        checks.append(_na_check("THIRD_PARTY_PAYMENT", "Cash transaction"))
        return
    if matched_txn is None:
        checks.append({"check_id": "THIRD_PARTY_PAYMENT", "result": "UNABLE_TO_VERIFY",
                        "evidence": [], "sop_clause": f"{sop_base} 5.1",
                        "codes": [], "hint": "PEND"})
        return

    paying_account = (matched_txn.get("remitter_account_masked", "") or "")[-4:]
    remitter_name = matched_txn.get("remitter_name", "")

    mandates, mname = store.lookup_mandates(folio_no)
    validated = [m for m in mandates if m.get("_status", "").strip().upper() == "VALIDATED"]
    if paying_account and any(m.get("account_no_masked", "").endswith(paying_account) for m in validated):
        checks.append({"check_id": "THIRD_PARTY_PAYMENT", "result": "PASS",
                        "evidence": [_make_evidence("bank_mandates", folio_no, f"Account {paying_account} validated")],
                        "sop_clause": f"{sop_base} 5.1",
                        "codes": [], "hint": None})
        return

    # Only first holder (and guardian on a minor folio) count as acceptable payers
    acceptable = [folio_row.get("first_holder_name", "").strip()]
    if folio_row.get("minor_dob", "").strip():
        guardian = folio_row.get("guardian_name", "").strip()
        if guardian:
            acceptable.append(guardian)
    acceptable = [n for n in acceptable if n]
    is_acceptable = _name_matches(remitter_name, acceptable)
    is_company = any(w in remitter_name.lower() for w in ["company", "corp", "ltd", "pvt", "limited"])

    if is_acceptable:
        has_proof = False
        for d, c in zip(docs, classifications):
            if c in ("bank statement", "transfer advice"):
                txt = (d.get("text", "") or "").lower()
                if paying_account in txt and remitter_name.lower() in txt and str(int(txn_amount)) in re.sub(r"[^0-9]", "", txt):
                    has_proof = True
                    break
        if has_proof:
            checks.append({"check_id": "THIRD_PARTY_PAYMENT", "result": "PASS",
                            "evidence": [], "sop_clause": f"{sop_base} 5.1",
                            "codes": [], "hint": None})
        else:
            checks.append({"check_id": "THIRD_PARTY_PAYMENT", "result": "UNABLE_TO_VERIFY",
                            "evidence": [], "sop_clause": f"{sop_base} 5.1",
                            "codes": ["BANK_PROOF_REQUIRED"], "hint": "PEND"})
        return

    if is_company and ("employee" in email_text.lower() or "payroll" in email_text.lower()):
        required = ["third-party payment declaration", "employer letter", "employee list", "KYC acknowledgement"]
        cls_count = {}
        for c in classifications:
            cls_count[c] = cls_count.get(c, 0) + 1
        missing = []
        for r in required:
            need = 2 if r == "KYC acknowledgement" else 1
            have = cls_count.get(r, 0)
            for _ in range(need - have):
                missing.append(r)
        if missing:
            checks.append({"check_id": "THIRD_PARTY_PAYMENT", "result": "FAIL",
                            "evidence": [_make_evidence("", "", f"Missing: {m}") for m in missing],
                            "sop_clause": f"{sop_base} 5.3",
                            "codes": ["TPP_DOCS_MISSING"], "hint": "PEND"})
        else:
            checks.append({"check_id": "THIRD_PARTY_PAYMENT", "result": "PASS",
                            "evidence": [], "sop_clause": f"{sop_base} 5.3",
                            "codes": [], "hint": None})
        return

    checks.append({"check_id": "THIRD_PARTY_PAYMENT", "result": "FAIL",
                    "evidence": [], "sop_clause": f"{sop_base} 5.2",
                    "codes": ["TPP_NO_EXCEPTION"], "hint": "REJECT"})


# ── Bank mandate ─────────────────────────────────────────────────────────────

def _bank_mandate_check(store, folio_no, matched_txn, request_type, last4_accounts,
                        payment_mode, docs, classifications, email_text, fund_house,
                        sop_base, addendum, checks):
    if request_type == "REDEMPTION":
        mandates, mname = store.lookup_mandates(folio_no)
        # Determine payout account from email-named last4, else default VALIDATED mandate
        named = None
        for l4 in last4_accounts:
            for m in mandates:
                if m.get("account_no_masked", "").endswith(l4):
                    named = m
                    break
            if named:
                break

        if named:
            st = named.get("_status", "").strip().upper()
            if st == "VALIDATED":
                checks.append({"check_id": "BANK_MANDATE", "result": "PASS",
                                "evidence": [_make_evidence("bank_mandates", folio_no, f"Named account {named.get('account_no_masked','')[-4:]} validated")],
                                "sop_clause": f"{sop_base} 6.2",
                                "codes": [], "hint": None})
                return
            else:
                checks.append({"check_id": "BANK_MANDATE", "result": "FAIL",
                                "evidence": [_make_evidence("bank_mandates", folio_no, f"Named account {named.get('account_no_masked','')[-4:]} status {st}")],
                                "sop_clause": f"{sop_base} 6.2",
                                "codes": [], "hint": "PROCEED_MODIFIED"})
                return
        # No named account → use default VALIDATED mandate
        for m in mandates:
            if m.get("_status", "").strip().upper() == "VALIDATED" and m.get("_is_default", False):
                checks.append({"check_id": "BANK_MANDATE", "result": "PASS",
                                "evidence": [_make_evidence("bank_mandates", folio_no, f"Default mandate {m.get('account_no_masked','')[-4:]} validated")],
                                "sop_clause": f"{sop_base} 6.2",
                                "codes": [], "hint": None})
                return
        for m in mandates:
            if m.get("_status", "").strip().upper() == "VALIDATED":
                checks.append({"check_id": "BANK_MANDATE", "result": "PASS",
                                "evidence": [_make_evidence("bank_mandates", folio_no, f"Mandate {m.get('account_no_masked','')[-4:]} validated")],
                                "sop_clause": f"{sop_base} 6.2",
                                "codes": [], "hint": None})
                return
        checks.append({"check_id": "BANK_MANDATE", "result": "FAIL",
                        "evidence": [_make_evidence("bank_mandates", folio_no, "No VALIDATED mandate")],
                        "sop_clause": f"{sop_base} 6.2",
                        "codes": [], "hint": "PROCEED_MODIFIED"})
        return

    if request_type == "CHANGE_OF_BANK":
        doc_cls = set(classifications)
        has_new_proof = any(c in doc_cls for c in ("cheque", "bank statement", "bank letter"))
        in_person = bool(re.search(r"in[- ]?person\s+verif", email_text, re.I))
        if fund_house == "NMF" and in_person:
            checks.append({"check_id": "BANK_MANDATE", "result": "PASS",
                            "evidence": [_make_evidence("", "", "COB in-person verification")],
                            "sop_clause": f"{sop_base} 6.3",
                            "codes": [], "hint": None})
            return
        if not has_new_proof:
            clause = f"{sop_base} 6.3"
            if fund_house == "TMF":
                clause += "; TMF-ADD-02 A2"
            checks.append({"check_id": "BANK_MANDATE", "result": "FAIL",
                            "evidence": [_make_evidence("", "", "Missing COB proof documents")],
                            "sop_clause": clause,
                            "codes": ["BANK_PROOF_REQUIRED"], "hint": "PEND"})
            return
        # Has new proof; check existing-account proof
        has_existing_proof = any(c in doc_cls for c in ("bank statement", "cheque", "bank letter"))
        if fund_house == "TMF" and not has_existing_proof:
            checks.append({"check_id": "BANK_MANDATE", "result": "FAIL",
                            "evidence": [_make_evidence("", "", "TMF requires existing-account proof")],
                            "sop_clause": f"{sop_base} 6.3; TMF-ADD-02 A2",
                            "codes": ["BANK_PROOF_REQUIRED"], "hint": "PEND"})
            return
        checks.append({"check_id": "BANK_MANDATE", "result": "PASS",
                        "evidence": [_make_evidence("", "", "COB proof documents present")],
                        "sop_clause": f"{sop_base} 6.3",
                        "codes": [], "hint": None})
        return

    checks.append(_na_check("BANK_MANDATE", "Not a redemption or change of bank"))


# ── COB cooling ──────────────────────────────────────────────────────────────

def _cob_cooling_check(store, folio_no, request_type, txn_date, sop_base, checks):
    if request_type != "REDEMPTION":
        checks.append(_na_check("COB_COOLING", "Not a redemption"))
        return
    if not txn_date:
        checks.append({"check_id": "COB_COOLING", "result": "UNABLE_TO_VERIFY",
                        "evidence": [], "sop_clause": f"{sop_base} 6.4",
                        "codes": [], "hint": "PEND"})
        return
    mandates, mname = store.lookup_mandates(folio_no)
    txn_dt = datetime.strptime(txn_date, "%Y-%m-%d").date()
    cooling = False
    cob_date = None
    for m in mandates:
        cd = m.get("_cob_date")
        if cd:
            cob = datetime.strptime(cd, "%Y-%m-%d").date()
            if 0 <= (txn_dt - cob).days <= 10:
                cooling = True
                cob_date = cob
                break
    if not cooling:
        checks.append({"check_id": "COB_COOLING", "result": "PASS",
                        "evidence": [], "sop_clause": f"{sop_base} 6.4",
                        "codes": [], "hint": None})
        return

    folio_row = store.lookup_folio(folio_no)[0]
    contact_changed = folio_row.get("contact_last_changed_on", "") if folio_row else ""
    if contact_changed:
        try:
            changed_dt = datetime.strptime(contact_changed[:10], "%Y-%m-%d").date()
            if cob_date and 0 <= (cob_date - changed_dt).days <= 30:
                checks.append({"check_id": "COB_COOLING", "result": "FAIL",
                                "evidence": [_make_evidence("folio_master", folio_no, f"Contact changed {contact_changed}")],
                                "sop_clause": f"{sop_base} 6.5",
                                "codes": ["HIGH_RISK_CONTACT_COB", "COB_COOLING"], "hint": "HOLD_AND_REFER"})
                return
        except:
            pass
    checks.append({"check_id": "COB_COOLING", "result": "FAIL",
                    "evidence": [_make_evidence("bank_mandates", folio_no, f"COB {cob_date}")],
                    "sop_clause": f"{sop_base} 6.4",
                    "codes": ["COB_COOLING"], "hint": "PEND"})


# ── Distributor code ─────────────────────────────────────────────────────────

def _distributor_check(store, matched_txn, request_type, arn, euin, fund_house,
                       txn_date, sop_base, checks):
    # NOT_APPLICABLE unless matched txn has plan=REGULAR and type is purchase/SIP reg
    if not matched_txn or matched_txn.get("plan", "").strip().upper() != "REGULAR" or request_type not in ("NEW_FOLIO_PURCHASE", "ADDITIONAL_PURCHASE", "SIP_REGISTRATION"):
        checks.append(_na_check("DISTRIBUTOR_CODE", "Not a regular-plan purchase or SIP registration"))
        return

    codes = []
    if not arn:
        checks.append({"check_id": "DISTRIBUTOR_CODE", "result": "FAIL",
                        "evidence": [_make_evidence("distributor_master", "", "ARN not provided")],
                        "sop_clause": f"{sop_base} 7.2",
                        "codes": ["ARN_INVALID_DIRECT"], "hint": "PROCEED_MODIFIED"})
        return

    dist_rows, dname = store.lookup_distributor(arn)
    if not dist_rows:
        checks.append({"check_id": "DISTRIBUTOR_CODE", "result": "FAIL",
                        "evidence": [_make_evidence("distributor_master", arn, "ARN not found")],
                        "sop_clause": f"{sop_base} 7.2",
                        "codes": ["ARN_INVALID_DIRECT"],
                        "hint": "PROCEED_MODIFIED"})
        return

    dist_row = max(dist_rows, key=lambda r: r.get("_valid_till_dt") or "")
    if dist_row.get("arn_status", "").strip().upper() != "ACTIVE":
        checks.append({"check_id": "DISTRIBUTOR_CODE", "result": "FAIL",
                        "evidence": [_make_evidence("distributor_master", arn, f"Status: {dist_row.get('arn_status')}")],
                        "sop_clause": f"{sop_base} 7.2",
                        "codes": ["ARN_INVALID_DIRECT"],
                        "hint": "PROCEED_MODIFIED"})
        return

    if dist_row.get("_valid_till_dt") and txn_date and dist_row["_valid_till_dt"] < txn_date:
        checks.append({"check_id": "DISTRIBUTOR_CODE", "result": "FAIL",
                        "evidence": [_make_evidence("distributor_master", arn, f"Expired {dist_row['_valid_till_dt']}")],
                        "sop_clause": f"{sop_base} 7.2",
                        "codes": ["ARN_INVALID_DIRECT"],
                        "hint": "PROCEED_MODIFIED"})
        return

    emp = dist_row.get("_empanelled_nmf" if fund_house == "NMF" else "_empanelled_tmf", False)
    if not emp:
        checks.append({"check_id": "DISTRIBUTOR_CODE", "result": "FAIL",
                        "evidence": [_make_evidence("distributor_master", arn, f"Not empanelled for {fund_house}")],
                        "sop_clause": f"{sop_base} 7.2",
                        "codes": ["ARN_INVALID_DIRECT"],
                        "hint": "PROCEED_MODIFIED"})
        return

    # ARN valid, empanelled, and EUIN blank → PASS with EUIN_MISSING code
    if not euin:
        checks.append({"check_id": "DISTRIBUTOR_CODE", "result": "PASS",
                        "evidence": [_make_evidence("distributor_master", arn, "ARN active, empanelled; EUIN missing")],
                        "sop_clause": f"{sop_base} 7.3",
                        "codes": ["EUIN_MISSING"], "hint": None})
        return

    checks.append({"check_id": "DISTRIBUTOR_CODE", "result": "PASS",
                    "evidence": [_make_evidence("distributor_master", arn, "ARN active, empanelled")],
                    "sop_clause": f"{sop_base} 7.2",
                    "codes": [], "hint": None})


# ── Minor rules ──────────────────────────────────────────────────────────────

def _minor_check(store, folio_row, matched_txn, sop_base, checks):
    dob = folio_row.get("minor_dob", "").strip() if folio_row else ""
    if not dob:
        checks.append(_na_check("MINOR_RULES", "Folio has no minor DOB"))
        return
    try:
        dob_dt = datetime.strptime(dob[:10], "%Y-%m-%d").date()
    except:
        checks.append(_na_check("MINOR_RULES", "Unable to parse DOB"))
        return
    txn_date = matched_txn["_txn_date"] if matched_txn else ""
    if not txn_date:
        checks.append(_na_check("MINOR_RULES", "No transaction date"))
        return
    try:
        txn_dt = datetime.strptime(txn_date, "%Y-%m-%d").date()
    except:
        checks.append(_na_check("MINOR_RULES", "Unable to parse txn date"))
        return
    age = txn_dt.year - dob_dt.year - ((txn_dt.month, txn_dt.day) < (dob_dt.month, dob_dt.day))
    if age >= 18:
        checks.append({"check_id": "MINOR_RULES", "result": "FAIL",
                        "evidence": [_make_evidence("folio_master", folio_row.get("folio_no", ""), f"Minor DOB {dob}, age {age}")],
                        "sop_clause": f"{sop_base} 5.5",
                        "codes": ["MINOR_ATTAINED_MAJORITY"], "hint": "PEND"})
        return
    if matched_txn:
        remitter = matched_txn.get("remitter_name", "")
        holders = _get_holders(folio_row)
        if not _name_matches(remitter, holders):
            checks.append({"check_id": "MINOR_RULES", "result": "FAIL",
                            "evidence": [_make_evidence("transactions", matched_txn.get("txn_id", ""), f"Payer: {remitter}")],
                            "sop_clause": f"{sop_base} 5.4",
                            "codes": [], "hint": "REJECT"})
            return
    checks.append({"check_id": "MINOR_RULES", "result": "PASS",
                    "evidence": [], "sop_clause": f"{sop_base} 5.4",
                    "codes": [], "hint": None})


# ── Referral ─────────────────────────────────────────────────────────────────

def _referral_check(store, folio_no, pan, fund_house, request_type, txn_date,
                    txn_amount, received_at, matched_txn, addendum, sop_base, checks):
    # Stop lock
    folio_row = store.lookup_folio(folio_no)[0]
    if folio_row and folio_row.get("folio_flag", "").strip() == "STOP_LOCK":
        checks.append({"check_id": "REFERRAL", "result": "FAIL",
                        "evidence": [_make_evidence("folio_master", folio_no, "STOP_LOCK")],
                        "sop_clause": f"{sop_base} 8.4",
                        "codes": ["STOP_LOCK"], "hint": "HOLD_AND_REFER"})
        return

    # TMF redemption >= 500000 within 30 days of purchase → addendum trigger
    if request_type == "REDEMPTION" and fund_house == "TMF" and txn_amount >= 500000:
        txns, _ = store.lookup_transactions(folio_no)
        if txn_date:
            txn_dt = datetime.strptime(txn_date, "%Y-%m-%d").date()
            purchase_evidence = []
            for t in txns:
                t_type = t.get("txn_type", "").strip()
                t_date = t.get("_txn_date", "")
                if t_type in ("NEW", "ADD") and t_date:
                    t_dt = datetime.strptime(t_date, "%Y-%m-%d").date()
                    if 0 <= (txn_dt - t_dt).days <= 30:
                        purchase_evidence.append(
                            _make_evidence("transactions", t.get("txn_id", ""),
                                           f"Purchase {t_date} INR {t['_amount']}"))
            if purchase_evidence and addendum:
                # Add redemption evidence
                red_ev = _make_evidence("transactions", matched_txn.get("txn_id", "") if matched_txn else "",
                                        f"Redemption {txn_date} INR {txn_amount}")
                all_ev = purchase_evidence + [red_ev]
                checks.append({"check_id": "REFERRAL", "result": "FAIL",
                                "evidence": all_ev,
                                "sop_clause": "TMF-ADD-02 A1",
                                "codes": ["REFERRAL_TRIGGER"], "hint": "HOLD_AND_REFER"})
                return

    # Purchase + redemption >= 2500000 within 14 days
    if txn_amount >= 2500000 and txn_date:
        txn_dt = datetime.strptime(txn_date, "%Y-%m-%d").date()
        txns, _ = store.lookup_transactions(folio_no)
        other_type = "RED" if request_type in ("NEW_FOLIO_PURCHASE", "ADDITIONAL_PURCHASE") else ("ADD" if request_type == "REDEMPTION" else None)
        if other_type:
            for t in txns:
                t_type = t.get("txn_type", "").strip()
                t_date = t.get("_txn_date", "")
                t_amt = t["_amount"]
                if t_type == other_type and t_date and t_amt >= 2500000:
                    t_dt = datetime.strptime(t_date, "%Y-%m-%d").date()
                    if abs((txn_dt - t_dt).days) <= 14:
                        ev = []
                        if request_type == "REDEMPTION":
                            ev.append(_make_evidence("transactions", t.get("txn_id", ""), f"Purchase {t_date} INR {t_amt}"))
                            ev.append(_make_evidence("transactions", matched_txn.get("txn_id", "") if matched_txn else "", f"Redemption {txn_date} INR {txn_amount}"))
                        else:
                            ev.append(_make_evidence("transactions", t.get("txn_id", ""), f"Redemption {t_date} INR {t_amt}"))
                            ev.append(_make_evidence("transactions", matched_txn.get("txn_id", "") if matched_txn else "", f"Purchase {txn_date} INR {txn_amount}"))
                        checks.append({"check_id": "REFERRAL", "result": "FAIL",
                                        "evidence": ev,
                                        "sop_clause": f"{sop_base} 8.3",
                                        "codes": ["REFERRAL_TRIGGER"], "hint": "HOLD_AND_REFER"})
                        return

    # Cash limit breach
    if request_type in _PURCHASE_LIKE:
        fy_start = received_at[:5] + "04-01" if received_at[5:7] >= "04" else f"{int(received_at[:4]) - 1}-04-01"
        total_cash, _ = store.get_cash_total(pan, fund_house, fy_start) if pan and fund_house else (0.0, "")
        if total_cash > 50000:
            checks.append({"check_id": "REFERRAL", "result": "FAIL",
                            "evidence": [_make_evidence("cash_total (aggregated by PAN/AMC/FY)", pan, f"Total cash: {total_cash}")],
                            "sop_clause": f"{sop_base} 8.5",
                            "codes": ["CASH_LIMIT_BREACH"], "hint": "REJECT"})
            return

    # Redemption with no matched row
    if request_type == "REDEMPTION" and matched_txn is None:
        checks.append({"check_id": "REFERRAL", "result": "UNABLE_TO_VERIFY",
                        "evidence": [], "sop_clause": f"{sop_base} 8.4",
                        "codes": [], "hint": "PEND"})
        return

    checks.append({"check_id": "REFERRAL", "result": "PASS",
                    "evidence": [], "sop_clause": f"{sop_base} 8.4",
                    "codes": [], "hint": None})