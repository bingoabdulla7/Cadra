"""src/intake.py — Document intake: decode, extract text, classify, extract facts, guard.

CONFIG limits attachment processing.
"""

from __future__ import annotations
import base64, io, json, os, re
from pathlib import Path
from typing import Any

# ── Config ───────────────────────────────────────────────────────────────────
CONFIG = {
    "max_attachments": 25,
    "max_size_mb": 20,
    "allowed_extensions": {".pdf", ".docx", ".xlsx", ".txt", ".png", ".jpg", ".jpeg", ".tiff", ".bmp"},
    "text_extensions": {".txt"},
    "image_extensions": {".png", ".jpg", ".jpeg", ".tiff", ".bmp"},
}

# ── Classifier labels ────────────────────────────────────────────────────────

DOC_TYPES = [
    "third-party payment declaration",
    "employer letter",
    "employee list",
    "KYC acknowledgement",
    "bank statement",
    "transfer advice",
    "cancelled cheque",
    "change-of-bank form",
]

REVIEW_TRIGGERS = [
    "ignore all rules",
    "ignore previous instructions",
    "ignore the previous",
    "system note",
    "automated reviewer",
    "return outcome",
    "output outcome",
    "disregard the sop",
]

# ── Document processing ──────────────────────────────────────────────────────

def decode_attachments(payload: str | list) -> list[dict]:
    """Decode a JSON string or list of base64-encoded attachments.
    Each item: {"file_name" or "filename": str, "content_base64": str, "content_type": str}.
    Returns list of decoded doc dicts with keys: filename, ext, raw_bytes, error, text, needs_vision, content_type.
    """
    if isinstance(payload, str):
        raw_list = json.loads(payload)
    else:
        raw_list = payload
    docs: list[dict] = []
    for item in raw_list[:CONFIG["max_attachments"]]:
        fname = item.get("file_name") or item.get("filename", "unnamed")
        b64 = item.get("content_base64", "")
        content_type = item.get("content_type", "")
        ext = Path(fname).suffix.lower()
        doc: dict[str, Any] = {
            "filename": fname,
            "ext": ext,
            "content_type": content_type,
            "raw_bytes": b"",
            "error": None,
            "text": "",
            "needs_vision": False,
        }
        try:
            raw = base64.b64decode(b64)
        except Exception as e:
            doc["error"] = f"base64 decode failed: {e}"
            docs.append(doc)
            continue

        # Use content_type as well as extension to decide allowed types
        allowed_ct = {"application/pdf", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                      "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                      "image/jpeg", "image/png", "image/tiff", "image/bmp", "text/plain"}
        if ext not in CONFIG["allowed_extensions"] and content_type not in allowed_ct:
            doc["error"] = f"unsupported extension {ext} or content_type {content_type}"
            doc["raw_bytes"] = raw
            docs.append(doc)
            continue

        if len(raw) > CONFIG["max_size_mb"] * 1024 * 1024:
            doc["error"] = f"file exceeds {CONFIG['max_size_mb']} MB"
            doc["raw_bytes"] = raw
            docs.append(doc)
            continue

        doc["raw_bytes"] = raw

        if ext in CONFIG["text_extensions"]:
            try:
                doc["text"] = raw.decode("utf-8", errors="replace")
            except Exception as e:
                doc["error"] = f"text decode failed: {e}"
        elif ext in CONFIG["image_extensions"]:
            doc["raw_bytes"] = raw
            doc["needs_vision"] = True
        elif ext == ".pdf":
            _process_pdf(doc, raw)
        elif ext == ".docx":
            _process_docx(doc, raw)
        elif ext == ".xlsx":
            _process_xlsx(doc, raw)
        docs.append(doc)
    return docs


def _process_pdf(doc: dict, raw: bytes):
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(raw))
        text_parts: list[str] = []
        for page in reader.pages:
            t = page.extract_text()
            if t and t.strip():
                text_parts.append(t)
        full = "\n".join(text_parts).strip()
        if full:
            doc["text"] = full
        else:
            doc["needs_vision"] = True  # no text layer
    except Exception as e:
        doc["error"] = f"PDF read failed: {e}"
        doc["needs_vision"] = True


def _process_docx(doc: dict, raw: bytes):
    try:
        from docx import Document
        docx_doc = Document(io.BytesIO(raw))
        paras = [p.text for p in docx_doc.paragraphs if p.text.strip()]
        # Also read tables
        for table in docx_doc.tables:
            for row in table.rows:
                cells = [c.text.strip() for c in row.cells]
                line = " | ".join(c for c in cells if c)
                if line:
                    paras.append(line)
        doc["text"] = "\n".join(paras)
    except Exception as e:
        doc["error"] = f"DOCX read failed: {e}"


def _process_xlsx(doc: dict, raw: bytes):
    try:
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        rows: list[str] = []
        for sheet in wb.worksheets:
            for row in sheet.iter_rows(values_only=True):
                line = " | ".join(str(c) if c is not None else "" for c in row)
                if line.strip():
                    rows.append(line)
        doc["text"] = "\n".join(rows)
    except Exception as e:
        doc["error"] = f"XLSX read failed: {e}"


def classify_document(doc: dict) -> str:
    """Classify document type based on filename and text content.
    Underscores in the filename are treated as spaces.
    """
    fname = doc.get("filename", "").lower().replace("_", " ")
    text = doc.get("text", "").lower()
    combined = fname + " " + text

    # third party payment declaration
    if "third party payment declaration" in combined or "tpp" in fname or "declaration" in fname:
        return "third-party payment declaration"

    # bank statement
    if "statement of account" in text:
        return "bank statement"

    # cancelled cheque
    if "cheque" in fname or "or bearer" in text:
        return "cancelled cheque"

    # change-of-bank form
    if "request for change of bank" in text or fname.startswith("cob"):
        return "change-of-bank form"

    # transfer advice
    if "funds transfer" in text or "transaction acknowledgement" in text:
        return "transfer advice"
    if "neft" in fname and "advice" in fname:
        return "transfer advice"

    # employer letter
    if "payroll" in text and "confirm" in text:
        return "employer letter"

    # employee list
    if "employee list" in fname:
        return "employee list"
    pan_pattern = re.findall(r"[A-Z]{5}\d{4}[A-Z]", text, re.IGNORECASE)
    if len(pan_pattern) >= 2 and "employee" in text:
        return "employee list"

    # KYC acknowledgement
    if "kyc" in fname:
        return "KYC acknowledgement"

    return "unknown"


def flag_reviewer_instructions(text: str) -> list[str]:
    """Flag any text that tries to instruct a reviewer to ignore rules."""
    found: list[str] = []
    lower = text.lower()
    for trigger in REVIEW_TRIGGERS:
        if trigger in lower:
            found.append(trigger)
    return found


# ── Plain-code extractor ─────────────────────────────────────────────────────

def extract_facts(text: str) -> dict[str, Any]:
    """Extract structured facts from document text.
    Returns dict with keys: folios, amounts, utr, arn, euin, last4_account,
    payment_mode, fund_house, request_type.
    """
    facts: dict[str, Any] = {
        "folios": [],
        "amounts": [],
        "utr": None,
        "arn": None,
        "euin": None,
        "last4_account": [],
        "payment_mode": None,
        "fund_house": None,
        "request_type": "unknown",
    }

    # Fund house from email sender name
    fh_lower = text.lower()
    if "narmada" in fh_lower or "nmf" in fh_lower:
        facts["fund_house"] = "NMF"
    elif "tungabhadra" in fh_lower or "tmf" in fh_lower:
        facts["fund_house"] = "TMF"

    # Folio numbers: canonical NNNNNNNN/NN starting 401 or 711
    folio_pat = re.findall(r"([471]\d{7,}?)", text)
    for raw in folio_pat:
        digits = re.sub(r"[^0-9]", "", raw)
        if len(digits) >= 10:
            canon = f"{digits[:8]}/{digits[8:10]}"
            if canon.startswith("401") or canon.startswith("711"):
                if canon not in facts["folios"]:
                    facts["folios"].append(canon)
    # Also find with slashes/hyphens
    folio_pat2 = re.findall(r"([4|7]\d{7}[\s\-/]*\d{2})", text)
    for raw in folio_pat2:
        digits = re.sub(r"[^0-9]", "", raw)
        if len(digits) >= 10:
            canon = f"{digits[:8]}/{digits[8:10]}"
            if canon.startswith("401") or canon.startswith("711"):
                if canon not in facts["folios"]:
                    facts["folios"].append(canon)

    # Folios deduplicate
    facts["folios"] = list(dict.fromkeys(facts["folios"]))

    # Amounts: Rs / INR / ₹ with Indian commas, lakh, crore → convert to float
    amt_patterns = [
        r"(?:Rs\.?\s*|INR\s*|₹)\s*([\d,]+(?:\.\d+)?)\s*(?:lakh|lacs|lac|crore|cr)?",
        r"([\d,]+(?:\.\d+)?)\s*(?:lakh|lacs|lac|crore|cr)",
    ]
    amounts_seen = set()
    for pat in amt_patterns:
        for m in re.finditer(pat, text, re.IGNORECASE):
            raw_amt = m.group(1).replace(",", "")  # remove commas
            suffix = (m.group(0).lower().split()[-1] if m.group(0).lower().split() else "")
            try:
                val = float(raw_amt)
            except ValueError:
                continue
            if "crore" in suffix or "cr" in suffix:
                val *= 10000000
            elif "lakh" in suffix or "lacs" in suffix or "lac" in suffix:
                val *= 100000
            # Round to 2 decimal places
            val = round(val, 2)
            if val not in amounts_seen:
                amounts_seen.add(val)
                facts["amounts"].append(val)

    # UTR: 4-6 capital letters + 9-16 digits, or "UPI" + 12 digits
    utr_match = re.search(r"\b([A-Z]{4,6}\d{9,16})\b", text)
    if utr_match:
        facts["utr"] = utr_match.group(1)
    upi_utr = re.search(r"\b(UPI\d{12})\b", text)
    if upi_utr and facts.get("utr") is None:
        facts["utr"] = upi_utr.group(1)

    # ARN: ARN- followed by 6-10 alphanumeric
    arn_m = re.search(r"ARN[-\s]?([A-Z0-9]{4,12})", text, re.IGNORECASE)
    if arn_m:
        facts["arn"] = "ARN-" + arn_m.group(1)

    # EUIN: E followed by exactly 6 digits
    euin_m = re.search(r"\bE(\d{6})\b", text)
    if euin_m:
        facts["euin"] = "E" + euin_m.group(1)

    # Last 4 digits of account numbers: only digits that follow
    # "ending", "a/c", "account", "acct", or a run of X/* characters
    patterns_last4 = [
        r"(?:ending|a/c|account|acct)[^\d]{0,10}(\d{4})(?:\D|$)",
        r"(?:X{4,}|\*{4,})(\d{4})",
    ]
    all_last4 = []
    for pat in patterns_last4:
        all_last4.extend(re.findall(pat, text, re.IGNORECASE))
    all_last4 = list(dict.fromkeys(all_last4))  # deduplicate, preserve order
    facts["last4_account"] = all_last4[:5]

    # Payment mode: detect words anywhere in text
    pm_text = text.lower()
    pm_order = [("neft", "NEFT"), ("rtgs", "RTGS"), ("imps", "IMPS"), ("upi", "UPI"),
                ("net banking", "NETBANKING"), ("nach", "NACH"),
                ("cheque", "CHEQUE"), ("cash", "CASH")]
    for word, code in pm_order:
        if word in pm_text:
            if code == "CHEQUE":
                # Ignore if it follows "cancelled"
                if re.search(r"cancelled\s+cheque", pm_text):
                    continue
            facts["payment_mode"] = code
            break

    # Request type
    facts["request_type"] = _detect_request_type(text)

    return facts


def _detect_request_type(text: str) -> str:
    t = text.lower()
    if any(w in t for w in ["redeem", "redemption", "withdraw"]):
        return "redemption"
    if any(w in t for w in ["nominee", "nomination", "address change"]):
        return "other"
    if any(w in t for w in ["redeem", "redemption", "withdraw"]):
        return "redemption"
    if "change of bank" in t or re.search(r"change (?:the |my |our )?registered bank", t) or re.search(r"replace (?:the |my |our )?registered bank", t) or re.search(r"update (?:the |my |our )?bank", t):
        # Check no investment wording present
        if not any(w in t for w in ["invest", "purchase", "additional", "folio"]):
            return "change of bank"
    if "new folio" in t or re.search(r"open\s+a?\s*.*folio", t):
        return "new folio"
    if re.search(r"\bsip\b", t) and any(w in t for w in ["register", "start", "new sip", "first instalment", "first installment"]):
        return "SIP registration"
    if re.search(r"\bsip\b", t) and any(w in t for w in ["instalment", "installment"]):
        return "SIP instalment"
    if any(w in t for w in ["additional", "add", "invest", "purchase", "transferred", "paid"]):
        return "additional purchase"
    return "unknown"


# ── Guard function ──────────────────────────────────────────────────────────

def guard_facts(facts: dict[str, Any], source_text: str) -> dict[str, Any]:
    """Remove any extracted fact value not found in the source text (raw email + attachments)."""
    lower = source_text.lower()
    guarded: dict[str, Any] = {
        "folios": [],
        "amounts": [],
        "utr": None,
        "arn": None,
        "euin": None,
        "last4_account": [],
        "payment_mode": None,
        "fund_house": facts.get("fund_house"),
        "request_type": facts.get("request_type", "unknown"),
    }
    for f in facts.get("folios", []):
        if f in lower or f.replace("/", "") in re.sub(r"[^0-9]", "", lower):
            guarded["folios"].append(f)
    for a in facts.get("amounts", []):
        # Use the same parser as extract_facts (Indian commas, lakh, crore)
        amt_patterns = [
            r"(?:Rs\.?\s*|INR\s*|₹)\s*([\d,]+(?:\.\d+)?)\s*(?:lakh|lacs|lac|crore|cr)?",
            r"([\d,]+(?:\.\d+)?)\s*(?:lakh|lacs|lac|crore|cr)",
        ]
        found = False
        for pat in amt_patterns:
            for m in re.finditer(pat, lower, re.IGNORECASE):
                raw_amt = m.group(1).replace(",", "")
                suffix = (m.group(0).lower().split()[-1] if m.group(0).lower().split() else "")
                try:
                    val = float(raw_amt)
                except ValueError:
                    continue
                if "crore" in suffix or "cr" in suffix:
                    val *= 10000000
                elif "lakh" in suffix or "lacs" in suffix or "lac" in suffix:
                    val *= 100000
                val = round(val, 2)
                if abs(a - val) < 0.001:
                    found = True
                    break
            if found:
                break
        if not found:
            # Fallback: raw digit match
            numeric_in_text = [float(x.replace(",", "")) for x in re.findall(r"\d[\d,]*(?:\.\d+)?", lower)]
            if any(abs(a - n) < 0.001 for n in numeric_in_text):
                found = True
        if found:
            guarded["amounts"].append(a)
    if facts.get("utr") and facts["utr"].lower() in lower:
        guarded["utr"] = facts["utr"]
    if facts.get("arn") and facts["arn"].lower() in lower:
        guarded["arn"] = facts["arn"]
    if facts.get("euin") and facts["euin"].lower() in lower:
        guarded["euin"] = facts["euin"]
    for l4 in facts.get("last4_account", []):
        if l4 in lower:
            guarded["last4_account"].append(l4)
    if facts.get("payment_mode") and facts["payment_mode"].lower() in lower:
        guarded["payment_mode"] = facts["payment_mode"]
    return guarded


# ── Top-level intake pipeline ────────────────────────────────────────────────

def intake_pipeline(payload: str, email_text: str = "") -> dict[str, Any]:
    """Run full intake pipeline: decode, classify, extract, guard.

    payload: JSON string of base64 attachments.
    email_text: raw email body (for fact guarding).
    Returns a dict with docs, classification, facts, guarded_facts, warnings.
    """
    docs = decode_attachments(payload)
    all_text = email_text + "\n".join(d.get("text", "") for d in docs)

    # Classify each doc
    classifications = [classify_document(d) for d in docs]

    # Reviewer instruction flags
    warnings = flag_reviewer_instructions(all_text)

    # Extract facts from all text
    facts = extract_facts(all_text)
    guarded = guard_facts(facts, all_text)

    return {
        "docs": docs,
        "classifications": classifications,
        "facts": facts,
        "guarded_facts": guarded,
        "warnings": warnings,
    }