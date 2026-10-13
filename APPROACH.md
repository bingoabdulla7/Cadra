# Approach

Entry point: this file links [FUTURE_WORKFLOW.md](FUTURE_WORKFLOW.md), [ARTEFACT.md](ARTEFACT.md), [README.md](README.md), [src/](src/) and [evaluation/](evaluation/).

## A. Request types

Seven types. Six are handled: NEW_FOLIO_PURCHASE, ADDITIONAL_PURCHASE, SIP_REGISTRATION, SIP_INSTALMENT, REDEMPTION and CHANGE_OF_BANK (proof presence only, not document content). OTHER covers switches, exchange transactions, nomination, transmission and FIU decisions (SOP 2.2) and is answered PEND.

## B. Agent design

A pipeline runs intake, one model call for facts only, a guard, deterministic checks, outcome and reply. The tools are the DataStore lookups (`lookup_folio`, `lookup_folio_by_pan`, `lookup_kra`, `lookup_mandates`, `lookup_transactions`, `lookup_distributor`, `get_cash_total`, `select_policy`) and `read_attachment`. Code calls them and lists them in `trace.tools_called`; each returns the record and its source. The model calls no tools and decides nothing. Policy is chosen from each document's own header. Failure paths are as in [FUTURE_WORKFLOW.md](FUTURE_WORKFLOW.md). The outcome is PROCEED only when every check passes.

## C. Data and grounding

A folio is resolved by exact canonical lookup, an investor by PAN, and the pending transaction of the same kind by UTR then amount, with identical twins collapsed. An email amount that matches no pending row means no transaction and UNABLE_TO_VERIFY. Any disagreement is UNABLE_TO_VERIFY, carrying the evidence (source file, record key, value) and the clause on the check.

## D. Evaluation design

A correct result is outcome, reason-code set, approver, request type and resolved folio and transaction all right with a valid contract. The costliest errors are unsafe ones: an outcome less restrictive than expected, such as PROCEED instead of REJECT, and tipping off. Over-cautious errors cost desk time. The bar before people review less is zero unsafe errors on a larger held-out set including real desk emails, plus agreement with checkers. Today that bar is not met.

## E. Risks and trade-offs

| risk | mitigation |
| --- | --- |
| Model misreads | Facts only, a guard, and code decides. |
| Instructions hidden in documents | Document text is treated as data. |
| Stale or missing sources | UNABLE_TO_VERIFY. |
| Model rate limits and slowness | Fallback chain, 25 s budget, rule-based fallback. |
| No pending register row | PEND, the safe answer. |
| Policy ambiguity | Judgement calls: new-folio Registered KYC is PEND per SOP 4.3 text; redemption in a cooling period after a contact change carries HIGH_RISK_CONTACT_COB and COB_COOLING; an own unregistered account with no proof is PEND with BANK_PROOF_REQUIRED; a named pending payout account is disregarded; OTHER is PEND. |

Given up: human review screen, persistent audit storage, reading document content beyond the model's transcription, rules live in code, replies are templates.
