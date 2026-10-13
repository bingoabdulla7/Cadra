# Artefact

## 1. Records held after preparation

After `python -m src.data` the store holds:

- 83 folios
- 522 transactions
- 105 bank mandates
- 95 KRA rows
- 32 distributor rows (24 unique ARNs)

Data problems and how each was handled:

- 56 transaction folio numbers were not in the required `NNNNNNNN/NN` form. The digits were extracted and re-formatted. None were substituted.
- 53 dates were written `dd-mm-yyyy`. Both formats were parsed.
- 4 duplicate transaction pairs shared a UTR. Identical twins were counted once, keeping the lowest id, and the twin was flagged.
- 6 KRA rows had blank status text. Status was taken from the code.
- 8 KRA codes were missing leading zeros.
- One PAN appeared on several folios, with names written differently. The investor was identified by PAN and folio.

## 2. Policy documents

- SOP v4.1 (current, effective 1 Aug 2026) applies to every request.
- TMF-ADD-02 (effective 15 Sep 2026) applies to TMF folios only.
- SOP v3.2 is superseded and is never selected.

Selection reads each document's own status, effective date and fund house against `received_at` in `src/data.py select_policy`. If no document is in force, the result is `UNABLE_TO_VERIFY`.

## 3. Evaluation

65 cases were written in the build session: 10 inbox requests, 8 for the other pending register rows, and 47 variants (rewordings, folio formats, identity failures, stale KYC, hidden instructions, contradictions, bank change, out of scope).

First measurement on the live service: 55.4% correct outcomes (36 of 65), 5 unsafe errors, 23 over-cautious errors, 1 request timed out.

After the changes below, measured on the live service: 55.4% (36 of 65), 5 unsafe, 24 over-cautious, no timeouts.

The outcome accuracy did not improve. Of the 29 mismatches: 11 are folios with no pending register row (the service answers PEND because it cannot confirm the payment, while the label expects PROCEED or a decision); 4 are cases with no folio in the data where the service says it cannot identify the folio; 2 are contested labels (C002 expects PROCEED for a grandparent's payment, which SOP 5.4 does not allow); and 12 are mixed: some labels contradict themselves (C003) and some are real gaps (a PAN quoted in the email is not compared with the folio, C038; payers who are not the first holder are not rejected when no matching payment row exists, C026 and C045). The set and the service were written by the same agent, and several labels were unrealistic.

Changes made after the first measurement:

- Every FAIL or UNABLE_TO_VERIFY now carries evidence and a clause.
- New-folio requests with Registered KYC now return `KYC_REGISTERED_NEW_AMC` using the target fund house.
- Change-of-bank wording is recognised.
- Replies give the cooling-hold date and a clean document list.
- The model merge no longer overwrites a correct ARN.
- A user-agent header fixed Groq HTTP 403 on Render.
- Model attempts are capped at 12 s with a 25 s budget.

These improved what the outcome metric does not measure.

## 4. Cost

Median about $0.0003 per request (list prices, unconfirmed; input $0.25 and output $1.50 per million tokens for Gemini flash-lite, $0.80 and $4.00 for Qwen). A 52-request run cost about $0.021. Median latency about 6 seconds. Of those requests, 34 were answered by Gemini 3.1 Flash Lite and 18 by Qwen3.8-27B through Groq.

## 5. Not built, fragile, and next steps

Not built:

- Human review screen.
- Persistent audit storage (the audit log is a local file on a free host).
- Reading of document content beyond the model's transcription.

Fragile:

- Free-tier model rate limits and slow responses.
- The free host sleeps after 15 minutes idle.
- Outcomes depend on my reading of ambiguous SOP clauses.
- Model names and prices change.

Next engineer, first:

- Relabel the 29 mismatches with the desk lead and re-run.
- Add held-out cases and real desk emails.
- Compare a PAN quoted in an email with the folio's PAN.
- Reject non-parent payers even when no payment row exists.
- Confirm model prices and limits.
- Add the review screen and persistent audit storage.
