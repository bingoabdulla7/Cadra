"""
src/data.py — Dataset loads, cleans, builds indexes and policy store.
Run: python -m src.data
Writes build/prep_report.json.
Never modifies Data/ files.
"""

from __future__ import annotations
import csv, json, os, re, sys
from pathlib import Path
from datetime import datetime, date
from collections import defaultdict
from typing import Any, Optional

# ── Constants ──────────────────────────────────────────────────────────────
DATA_DIR = Path(__file__).resolve().parent.parent / "Data"
BUILD_DIR = Path(__file__).resolve().parent.parent / "build"

# ── Helpers ─────────────────────────────────────────────────────────────────

def _canonical_folio(raw: str) -> str:
    """Strip non-digits, then insert slash after first 8 digits → NNNNNNNN/NN."""
    digits = re.sub(r"[^0-9]", "", raw)
    if len(digits) == 10:
        return f"{digits[:8]}/{digits[8:]}"
    return digits  # fallback — caller will treat as not found


def _parse_date(raw: str) -> str | None:
    """Return YYYY-MM-DD if parseable, else None.
    Accepts yyyy-mm-dd, dd-mm-yyyy, dd/mm/yyyy, dd Mon YYYY (from PDFs)."""
    raw = raw.strip()
    # already YYYY-MM-DD
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", raw)
    if m:
        return raw
    # DD-MM-YYYY or DD/MM/YYYY
    m = re.match(r"^(\d{2})[-/](\d{2})[-/](\d{4})$", raw)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    # DD Mon YYYY e.g. "01 Aug 2026" or "01 August 2026"
    m = re.match(r"^(\d{1,2})\s+([A-Za-z]{3,})\s+(\d{4})$", raw)
    if m:
        MONTHS = {"jan":1,"january":1,"feb":2,"february":2,"mar":3,"march":3,
                  "apr":4,"april":4,"may":5,"jun":6,"june":6,
                  "jul":7,"july":7,"aug":8,"august":8,"sep":9,"september":9,
                  "oct":10,"october":10,"nov":11,"november":11,"dec":12,"december":12}
        mon = MONTHS.get(m.group(2).lower()[:4])
        if mon:
            return f"{m.group(3)}-{mon:02d}-{int(m.group(1)):02d}"
    return None


def _kra_status_from_code(code_str: str) -> str:
    """Maps kra_status_code to one of: VALIDATED, REGISTERED, ON_HOLD, REJECTED, UNABLE_TO_VERIFY.
    Logic: take the last two digits of the (padded) numeric code."""
    digits = re.sub(r"[^0-9]", "", code_str)
    if not digits:
        return "UNKNOWN"
    # pad to at least 2 digits
    while len(digits) < 2:
        digits = "0" + digits
    last_two = digits[-2:]
    mapping = {
        "07": "VALIDATED",
        "02": "REGISTERED",
        "03": "ON_HOLD",
        "04": "REJECTED",
        "01": "UNABLE_TO_VERIFY",
        "05": "UNABLE_TO_VERIFY",
        "99": "UNABLE_TO_VERIFY",
    }
    return mapping.get(last_two, "UNKNOWN")


# ── Data store ──────────────────────────────────────────────────────────────

class DataStore:
    """Holds all loaded and cleaned data; provides lookup functions."""

    def __init__(self):
        self.records: dict[str, Any] = {}
        self.issues: list[dict] = []
        self._folios: dict[str, dict] = {}          # canonical folio -> row
        self._folios_by_pan: dict[str, list[str]] = defaultdict(list)
        self._kra_by_pan: dict[str, dict] = {}
        self._mandates_by_folio: dict[str, list[dict]] = defaultdict(list)
        self._txns_by_folio: dict[str, list[dict]] = defaultdict(list)
        self._txns_raw: list[dict] = []
        self._distributors: dict[str, list[dict]] = defaultdict(list)  # ARN -> rows
        self._schemes_by_folio: dict[str, str] = {}   # folio -> scheme info
        self._policies: list[dict] = []
        self.policy_selected: dict | None = None      # for trace

    # ── Load ────────────────────────────────────────────────────────────

    def load_all(self):
        self._load_folios()
        self._load_kra()
        self._load_transactions()
        self._load_mandates()
        self._load_distributors()
        self._load_policies()
        self._build_indexes()

    # ── Folio master ────────────────────────────────────────────────────

    def _load_folios(self):
        path = DATA_DIR / "data" / "folio_master.csv"
        rows = self._csv(path)
        self.records["folio_master_count"] = len(rows)
        for r in rows:
            folio = _canonical_folio(r["folio_no"])
            if folio in self._folios:
                self.issues.append({
                    "source": "folio_master", "row": r["folio_no"],
                    "issue": f"Duplicate folio after canonicalisation: {folio}"})
            self._folios[folio] = r
        self._log_count("folio_master", len(self._folios))

    # ── KRA ─────────────────────────────────────────────────────────────

    def _load_kra(self):
        path = DATA_DIR / "data" / "kra_kyc_snapshot.csv"
        rows = self._csv(path)
        self.records["kra_count"] = len(rows)
        for r in rows:
            pan = r["pan_masked"].strip()
            code = r.get("kra_status_code", "").strip()
            status = _kra_status_from_code(code)
            r["_derived_kra_status"] = status
            # parse last_checked_at
            lca = r.get("last_checked_at", "").strip()
            r["_last_checked_dt"] = lca  # keep as string YYYY-MM-DD HH:MM
            email_v = r.get("email_validated", "").strip().upper()
            mobile_v = r.get("mobile_validated", "").strip().upper()
            r["_email_ok"] = email_v == "Y"
            r["_mobile_ok"] = mobile_v == "Y"
            # detect code 399 etc from kra_status text
            if "not reachable" in r.get("kra_status", "").lower() or "unable" in r.get("kra_status", "").lower():
                r["_derived_kra_status"] = "UNABLE_TO_VERIFY"
            self._kra_by_pan[pan] = r
        self._log_count("kra", len(self._kra_by_pan))

    # ── Transactions ────────────────────────────────────────────────────

    def _load_transactions(self):
        path = DATA_DIR / "data" / "transactions.csv"
        rows = self._csv(path)
        self.records["transactions_count"] = len(rows)
        # deduplicate — only flag when UTR is non-empty and all key fields match
        seen: dict = {}  # key -> first txn_id
        dupes = 0
        for r in rows:
            raw_folio = r["folio_no"].strip()
            folio = _canonical_folio(raw_folio)
            r["_folio"] = folio
            amt_raw = r.get("amount_inr", "0").strip()
            r["_amount"] = float(amt_raw) if amt_raw else 0.0
            txn_date = _parse_date(r.get("txn_date", ""))
            r["_txn_date"] = txn_date or ""
            r["_txn_date_dt"] = _parse_date_to_date(txn_date or r.get("txn_date", ""))

            utr = r.get("utr", "").strip()
            remitter = r.get("remitter_account_masked", "").strip()
            # Only flag as duplicate if UTR is present (recurring SIPs have no UTR)
            if utr:
                key = (folio, amt_raw, utr, remitter)
                if key in seen:
                    dupes += 1
                    self.issues.append({
                        "source": "transactions", "txn_id": r["txn_id"],
                        "issue": f"Duplicate transaction (same UTR): folio={folio} amt={amt_raw} utr={utr} first={seen[key]}"})
                else:
                    seen[key] = r["txn_id"]
            self._txns_raw.append(r)
            self._txns_by_folio[folio].append(r)
        self.records["duplicate_txns_flagged"] = dupes
        self.records["transactions_unique"] = len(self._txns_raw)
        self._log_count("transactions", len(self._txns_raw))

    # ── Bank mandates ───────────────────────────────────────────────────

    def _load_mandates(self):
        path = DATA_DIR / "data" / "bank_mandates.csv"
        rows = self._csv(path)
        self.records["bank_mandates_count"] = len(rows)
        for r in rows:
            folio = _canonical_folio(r["folio_no"])
            r["_folio"] = folio
            is_def = r.get("is_default", "").strip().upper() == "Y"
            r["_is_default"] = is_def
            r["_status"] = r.get("status", "").strip()
            r["_cob_date"] = _parse_date(r.get("cob_request_received_on", ""))
            self._mandates_by_folio[folio].append(r)
        self._log_count("bank_mandates", len(rows))

    # ── Distributors ────────────────────────────────────────────────────

    def _load_distributors(self):
        path = DATA_DIR / "data" / "distributor_master.csv"
        rows = self._csv(path)
        self.records["distributor_count"] = len(rows)
        for r in rows:
            arn = r["arn"].strip()
            r["_valid_till_dt"] = _parse_date(r.get("arn_valid_till", ""))
            r["_empanelled_nmf"] = r.get("empanelled_nmf", "").strip().upper() == "Y"
            r["_empanelled_tmf"] = r.get("empanelled_tmf", "").strip().upper() == "Y"
            self._distributors[arn].append(r)
        self._log_count("distributors", len(self._distributors))

    # ── Policies ────────────────────────────────────────────────────────

    def _load_policies(self):
        policies_dir = DATA_DIR / "policies"
        self.records["policies_loaded"] = []
        for fname in sorted(policies_dir.iterdir()):
            if fname.suffix.lower() in (".docx", ".pdf"):
                info = self._extract_policy_header(fname)
                if info:
                    self._policies.append(info)
                    self.records["policies_loaded"].append(f"{info['id']} v{info['version']} ({info['status']})")
        self._log_count("policies", len(self._policies))

    def _extract_policy_header(self, path: Path) -> dict | None:
        """Open a docx or PDF and read the header table/fields to extract:
        id, version, effective_date, status, fund_house, clauses dict."""
        info: dict = {"path": str(path), "id": "", "version": "",
                      "effective_date": "", "status": "",
                      "fund_house": "", "clauses": {}}
        if path.suffix.lower() == ".docx":
            from docx import Document  # type: ignore
            doc = Document(str(path))
            # first table usually contains header — only read the first table for header fields
            labels = {}
            if doc.tables:
                for row in doc.tables[0].rows:
                    cells = [c.text.strip() for c in row.cells]
                    if len(cells) >= 2:
                        labels[cells[0].lower()] = cells[1]
                if "document id" in labels:
                    info["id"] = labels["document id"]
                if "version" in labels:
                    info["version"] = labels["version"]
                if "effective date" in labels:
                    info["effective_date"] = _parse_date(labels["effective date"]) or labels["effective date"]
                if "status" in labels:
                    s = labels["status"]
                    info["status"] = s
                    # parse status for approved/current
                    if "approved" in s.lower() or "current" in s.lower():
                        info["_is_approved"] = True
                    else:
                        info["_is_approved"] = False
                if "applies to" in labels:
                    info["fund_house"] = labels["applies to"]
            # extract clauses from paragraphs
            clauses: dict[str, str] = {}
            current_clause = ""
            current_text: list[str] = []
            for p in doc.paragraphs:
                txt = p.text.strip()
                if not txt:
                    continue
                # Try matching clause header e.g. "4.1", "5.3"
                m = re.match(r"^(\d+\.\d+)\s", txt)
                if m:
                    if current_clause:
                        clauses[current_clause] = " ".join(current_text)
                    current_clause = m.group(1)
                    current_text = [txt]
                elif current_clause:
                    # check if it's a clause that doesn't start with number (continuation)
                    current_text.append(txt)
            if current_clause:
                clauses[current_clause] = " ".join(current_text)
            info["clauses"] = clauses
        elif path.suffix.lower() == ".pdf":
            from pypdf import PdfReader  # type: ignore
            reader = PdfReader(str(path))
            text = ""
            for page in reader.pages:
                text += page.extract_text() + "\n"
            # Parse header fields from PDF text
            lines = text.split("\n")
            for i, line in enumerate(lines):
                ls = line.strip()
                if "Document ID" in ls and i + 1 < len(lines):
                    info["id"] = lines[i + 1].strip()
                elif "Version" in ls and i + 1 < len(lines):
                    info["version"] = lines[i + 1].strip()
                elif "Effective date" in ls and i + 1 < len(lines):
                    ed = lines[i + 1].strip()
                    info["effective_date"] = _parse_date(ed) or ed
                elif "Status" in ls and i + 1 < len(lines):
                    st = lines[i + 1].strip()
                    info["status"] = st
                    info["_is_approved"] = "superseded" not in st.lower()
                elif "Applies to" in ls and i + 1 < len(lines):
                    info["fund_house"] = lines[i + 1].strip()
            # Extract clauses from PDF text
            clauses = {}
            # lines containing clause numbers like "4.1" at the start
            current_clause = ""
            current_text = []
            for line in lines:
                ls = line.strip()
                if not ls:
                    continue
                m = re.match(r"^(\d+\.\d+)\s", ls)
                if m:
                    if current_clause:
                        clauses[current_clause] = " ".join(current_text)
                    current_clause = m.group(1)
                    current_text = [ls]
                elif current_clause:
                    current_text.append(ls)
            if current_clause:
                clauses[current_clause] = " ".join(current_text)
            info["clauses"] = clauses

        if not info["id"] and not info["version"]:
            # fallback: use filename
            info["id"] = path.stem
            info["version"] = "?"
        return info

    # ── Index building ──────────────────────────────────────────────────

    def _build_indexes(self):
        """Map PAN → list of folios."""
        for folio, row in self._folios.items():
            pan = row["first_holder_pan_masked"].strip()
            if pan:
                self._folios_by_pan[pan].append(folio)

    # ── Lookup functions (each returns result and records its name for trace) ──

    def lookup_folio(self, folio: str) -> tuple[dict | None, str]:
        """Returns (folio_row or None, lookup_name)."""
        cf = _canonical_folio(folio)
        return self._folios.get(cf), "folio_master"

    def lookup_folio_by_pan(self, pan: str) -> tuple[list[str], str]:
        """Returns (list of folios for PAN, lookup_name)."""
        return self._folios_by_pan.get(pan, []), "folio_by_pan"

    def lookup_kra(self, pan: str) -> tuple[dict | None, str]:
        """Returns (kra_row or None, lookup_name)."""
        return self._kra_by_pan.get(pan), "kra_kyc_snapshot"

    def lookup_mandates(self, folio: str) -> tuple[list[dict], str]:
        """Returns (list of mandates for folio, lookup_name)."""
        cf = _canonical_folio(folio)
        return self._mandates_by_folio.get(cf, []), "bank_mandates"

    def lookup_transactions(self, folio: str) -> tuple[list[dict], str]:
        """Returns (list of transactions for folio, lookup_name)."""
        cf = _canonical_folio(folio)
        return self._txns_by_folio.get(cf, []), "transactions"

    def lookup_distributor(self, arn: str) -> tuple[list[dict], str]:
        """Returns (list of ARN rows, lookup_name)."""
        return self._distributors.get(arn, []), "distributor_master"

    def get_cash_total(self, pan: str, fund_house: str, fy_start: str) -> tuple[float, str]:
        """Sum all CASH transactions (PROCESSED + PENDING_TCC) for this PAN across all
        their folios in the given fund_house, for transactions on or after fy_start.
        Returns (total_cash, lookup_name)."""
        total = 0.0
        folios = self._folios_by_pan.get(pan, [])
        for folio in folios:
            frow = self._folios.get(folio, {})
            if frow.get("amc_code", "").strip() != fund_house:
                continue
            txns = self._txns_by_folio.get(folio, [])
            for t in txns:
                if t.get("payment_mode", "").strip().upper() != "CASH":
                    continue
                if t["_txn_date"] < fy_start:
                    continue
                total += t["_amount"]
        return total, "cash_total (aggregated by PAN/AMC/FY)"

    def select_policy(self, received_at: str, fund_house: str) -> tuple[dict | None, list[str]]:
        """Select the SOP and addendum in force for received_at and fund_house.
        Returns (combined_policy_dict or None, [document_ids_used]).
        Criteria: An SOP that is Approved (status), is in force at received_at,
        and applies to the fund_house. Never use a superseded version.
        Also apply addendum for TMF."""
        rdate = _parse_date(received_at) or received_at[:10]
        doc_ids: list[str] = []

        # Find the Approved, Current SOP that applies to this fund house
        selected_sop = None
        for pol in self._policies:
            if not pol.get("_is_approved"):
                continue
            if "sop" not in pol.get("id", "").lower():
                continue
            # Check if it applies to this fund house
            fh = pol.get("fund_house", "").lower()
            if fund_house.upper() == "NMF" and "narmada" not in fh and "tungabhadra" not in fh:
                continue
            if fund_house.upper() == "TMF" and "tungabhadra" not in fh and "narmada" not in fh:
                continue
            # Check effective date <= received_at
            eff_date = pol.get("effective_date", "")
            if eff_date and eff_date <= rdate:
                if selected_sop is None or pol.get("version", "0") > selected_sop.get("version", "0"):
                    selected_sop = pol

        if selected_sop:
            doc_ids.append(f"{selected_sop['id']} v{selected_sop['version']}")

        # Find addendum for TMF
        selected_addendum = None
        if fund_house.upper() == "TMF":
            for pol in self._policies:
                if "addendum" not in pol.get("id", "").lower():
                    continue
                eff_date = pol.get("effective_date", "")
                if eff_date and eff_date <= rdate:
                    if selected_addendum is None or eff_date > selected_addendum.get("effective_date", ""):
                        selected_addendum = pol
            if selected_addendum:
                doc_ids.append(f"{selected_addendum['id']}")

        # Build combined
        if selected_sop is None:
            return None, doc_ids

        combined = dict(selected_sop)
        combined["sop"] = selected_sop
        combined["addendum"] = selected_addendum
        self.policy_selected = combined
        return combined, doc_ids

    # ── Report ──────────────────────────────────────────────────────────

    def write_report(self):
        BUILD_DIR.mkdir(parents=True, exist_ok=True)
        report = {
            "record_counts": self.records,
            "data_issues": self.issues,
            "issues_count": len(self.issues),
            "policies_available": [
                {"id": p["id"], "version": p["version"], "status": p["status"],
                 "effective_date": p["effective_date"],
                 "applicable_to": p["fund_house"]}
                for p in self._policies
            ],
        }
        path = BUILD_DIR / "prep_report.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, default=str)
        print(f"Written {path}")

    # ── Internal ────────────────────────────────────────────────────────

    @staticmethod
    def _csv(path: Path) -> list[dict[str, str]]:
        with open(path, newline="", encoding="utf-8-sig") as f:
            return [row for row in csv.DictReader(f)]

    @staticmethod
    def _log_count(name: str, n: int):
        print(f"  {name:20s} {n:>6d}")


def _parse_date_to_date(raw: str) -> date | None:
    """Convert YYYY-MM-DD to date object for comparison."""
    try:
        return datetime.strptime(raw[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


# ── Main ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Loading data from", DATA_DIR)
    store = DataStore()
    store.load_all()
    store.write_report()
    print(f"\nDone. {len(store.issues)} issue(s) found.")
    for iss in store.issues:
        print(f"  [{iss['source']}] {iss.get('txn_id', '')} {iss['issue']}")