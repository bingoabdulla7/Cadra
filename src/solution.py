"""F1 capstone solution entry point — FastAPI app."""

from __future__ import annotations
import json, os, sys, re, uuid
from pathlib import Path
from datetime import datetime
from typing import Any
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.data import DataStore, DATA_DIR, BUILD_DIR
from src.intake import intake_pipeline
from src.llm import enrich
from src.checks import evaluate
from src.outcome import decide

store: DataStore | None = None
_tools_log: list[str] = []


class WrappedStore:
    """Wraps DataStore to log every lookup call for tools_called trace."""

    def __init__(self, inner: DataStore):
        self._inner = inner
        self._tools_log = []

    @property
    def policy_selected(self):
        return self._inner.policy_selected

    def _log(self, name: str):
        self._tools_log.append(name)

    def lookup_folio(self, folio: str):
        self._log("lookup_folio")
        return self._inner.lookup_folio(folio)

    def lookup_folio_by_pan(self, pan: str):
        self._log("lookup_folio_by_pan")
        return self._inner.lookup_folio_by_pan(pan)

    def lookup_kra(self, pan: str):
        self._log("lookup_kra")
        return self._inner.lookup_kra(pan)

    def lookup_mandates(self, folio: str):
        self._log("lookup_mandates")
        return self._inner.lookup_mandates(folio)

    def lookup_transactions(self, folio: str):
        self._log("lookup_transactions")
        return self._inner.lookup_transactions(folio)

    def lookup_distributor(self, arn: str):
        self._log("lookup_distributor")
        return self._inner.lookup_distributor(arn)

    def get_cash_total(self, pan: str, fund_house: str, fy_start: str):
        self._log("get_cash_total")
        return self._inner.get_cash_total(pan, fund_house, fy_start)

    def select_policy(self, received_at: str, fund_house: str):
        self._log("select_policy")
        return self._inner.select_policy(received_at, fund_house)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global store
    inner = DataStore()
    inner.load_all()
    store = WrappedStore(inner)
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(title="TCC Triage", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post(
    "/requests",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "request_id": {"type": "string"},
                            "received_at": {"type": "string"},
                            "channel": {"type": "string"},
                            "from": {"type": "string"},
                            "to": {"type": "string"},
                            "cc": {"type": "string"},
                            "subject": {"type": "string"},
                            "body": {"type": "string"},
                            "attachments": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "file_name": {"type": "string"},
                                        "content_type": {"type": "string"},
                                        "content_base64": {"type": "string"},
                                    },
                                },
                            },
                        },
                    },
                    "example": {
                        "request_id": "REQ-1001",
                        "received_at": "2024-06-01T09:30:00Z",
                        "channel": "email",
                        "from": "investor@example.com",
                        "to": "service@example.com",
                        "cc": "",
                        "subject": "Statement request",
                        "body": "Please share my account statement.",
                        "attachments": [
                            {
                                "file_name": "request.pdf",
                                "content_type": "application/pdf",
                                "content_base64": "JVBERi0xLjQK...",
                            }
                        ],
                    },
                }
            },
        }
    },
)
async def handle_request(raw: Request):
    global store
    wstore = store
    wstore._tools_log = []
    tools_called: list[str] = []
    documents_used: list[dict] = []
    flags: list[str] = []
    try:
        body = await raw.json()
    except Exception:
        return _error_response("", "Invalid JSON body")

    try:
        request_id = str(body.get("request_id", ""))
        req = {}
        req["received_at"] = str(body.get("received_at", ""))
        req["from"] = str(body.get("from", ""))
        req["subject"] = str(body.get("subject", ""))
        req["body"] = str(body.get("body", ""))

        attachments = body.get("attachments", "[]")
        email_full = req["subject"] + "\n" + req["body"]

        intake = intake_pipeline(attachments, email_full)

        intake, model_calls, llm_notes = enrich(req, intake)

        num_attachments = 0
        try:
            pl = json.loads(attachments) if isinstance(attachments, str) else attachments
            num_attachments = len(pl) if isinstance(pl, list) else 0
        except Exception:
            pass
        if num_attachments > 0:
            tools_called.append("read_attachment")

        for d in intake.get("docs", []):
            if d.get("error"):
                flags.append(f"Cannot read attachment: {d.get('filename', 'unknown')}")

        if intake.get("warnings"):
            flags.extend(intake["warnings"])
        flags.extend(llm_notes)

        result = evaluate(wstore, req, intake)
        decision = decide(result, req)

        tools_called.extend(wstore._tools_log)

        # Build documents_used from policy_docs
        for pd in result.get("policy_docs", []):
            parts = pd.split(" v", 1)
            doc_name = parts[0]
            doc_ver = parts[1] if len(parts) > 1 else ""
            documents_used.append({"document": doc_name, "version": doc_ver})

        # Build checks without internal fields
        checks_out = []
        for c in result.get("checks", []):
            checks_out.append({
                "check_id": c.get("check_id", ""),
                "result": c.get("result", ""),
                "evidence": c.get("evidence", []),
                "sop_clause": c.get("sop_clause", ""),
            })

        resolved = result.get("resolved", {})
        dup_ids = result.get("duplicate_txn_ids", [])
        if dup_ids:
            flags.append(f"Duplicate transaction notice: {', '.join(dup_ids)}")

        resp = {
            "request_id": request_id,
            "request_type": result.get("request_type", "unknown"),
            "resolved": {
                "folio_no": resolved.get("folio_no"),
                "amc_code": resolved.get("amc_code"),
                "first_holder_pan_masked": resolved.get("first_holder_pan_masked"),
                "txn_id": resolved.get("txn_id"),
            },
            "outcome": decision.get("outcome", "PEND"),
            "reason_codes": decision.get("reason_codes", []),
            "checks": checks_out,
            "human_approval_required": decision.get("human_approval_required", False),
            "approver_role": decision.get("approver_role"),
            "draft_reply": decision.get("draft_reply", {"to": "", "subject": "", "body": ""}),
            "trace": {
                "model_calls": model_calls,
                "tools_called": tools_called,
                "documents_used": documents_used,
                "cost_usd": sum(
                    (mc["input_tokens"] / 1_000_000) * 0.25 +
                    (mc["output_tokens"] / 1_000_000) * 1.50
                    if "gemini-3.5" in mc["model"] or "gemini-3.1" in mc["model"]
                    else (mc["input_tokens"] / 1_000_000) * 0.80 +
                    (mc["output_tokens"] / 1_000_000) * 4.00
                    if "qwen" in mc["model"]
                    else (mc["input_tokens"] / 1_000_000) * 0.15 +
                    (mc["output_tokens"] / 1_000_000) * 0.75
                    for mc in model_calls
                ),
            },
            "draft_status": "draft",
            "review_flags": flags,
            "decision_basis": decision.get("decision_basis", []),
            "notes": "",
        }

        # Log to case_log.jsonl
        log_entry = {
            "request_id": request_id,
            "outcome": decision.get("outcome", ""),
            "reason_codes": decision.get("reason_codes", []),
            "folio_no": resolved.get("folio_no"),
            "txn_id": resolved.get("txn_id"),
            "policy_documents": result.get("policy_docs", []),
        }
        _append_log(log_entry)

        return resp

    except Exception as e:
        return _error_response(request_id, str(e))


def _error_response(request_id: str, note: str) -> dict:
    err_resp = {
        "request_id": request_id,
        "request_type": "OTHER",
        "resolved": {"folio_no": None, "amc_code": None,
                     "first_holder_pan_masked": None, "txn_id": None},
        "outcome": "PEND",
        "reason_codes": [],
        "checks": [
            {"check_id": "FOLIO_IDENTITY", "result": "UNABLE_TO_VERIFY",
             "evidence": [{"source": "", "ref": "", "value": note}],
             "sop_clause": ""}
        ],
        "human_approval_required": False,
        "approver_role": None,
        "draft_reply": {"to": "", "subject": "", "body": ""},
        "trace": {"model_calls": [], "tools_called": [], "documents_used": [], "cost_usd": 0.0},
        "draft_status": "draft",
        "review_flags": [note] if note else [],
        "decision_basis": [],
        "notes": note,
    }
    return err_resp


def _append_log(entry: dict):
    try:
        log_path = BUILD_DIR / "case_log.jsonl"
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def main():
    import uvicorn
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("src.solution:app", host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()