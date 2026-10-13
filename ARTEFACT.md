# Artefact

Deployed service: https://tcc-desk-agent.onrender.com

## What it is

A service that answers distributor/agent desk email requests about folios, transactions, KYC and bank mandates. It runs on deterministic rules over prepared records, with one guarded model call to help extraction. It returns an outcome plus the extra fields `draft_status`, `review_flags`, `decision_basis` and `notes`.

## Run it

- Prepare data: `python -m src.data`
- Run: `python -m src.solution` (reads `PORT`)
- Environment variables: `GEMINI_API_KEY`, `GROQ_API_KEY`, `LLM_MODE`

See [README.md](README.md).

## Records held after preparation

83 folios, 522 transactions, 105 bank mandates, 95 KRA rows and 32 distributor rows (24 unique ARNs).

## Data problems found and handled

- 56 transaction folio numbers not in `NNNNNNNN/NN` form
- 53 dates written `dd-mm-yyyy`
- 4 duplicate transaction pairs sharing a UTR
- 6 KRA rows with blank status text (status taken from the code) and 8 codes missing leading zeros
- one PAN on several folios and names written differently

## Policy documents

SOP v4.1 (current, effective 1 Aug 2026) and TMF-ADD-02 (effective 15 Sep 2026, TMF only). SOP v3.2 is superseded and never selected. Selection reads each document's own status and effective date against `received_at` (`src/data.py`, `select_policy`). Rules live in `src/checks.py` and `src/outcome.py`; the workflow is in [FUTURE_WORKFLOW.md](FUTURE_WORKFLOW.md) and the approach in [APPROACH.md](APPROACH.md).

## Evaluation

65 cases written in this session. First accuracy measured on the live service was 55.4% (36 of 65), with 5 unsafe errors and 23 over-cautious errors. Many cases use folios with no pending register row, so the service answers `PEND` because it cannot confirm the payment; some labels were unrealistic; and the set was written by the same agent that wrote the service. Median cost per request is about $0.0002 with Gemini flash-lite (list prices, unconfirmed); median latency about 5 seconds. Cases are in [evaluation/](evaluation/).

## Known problems

- Groq calls return HTTP 403 on Render; a model attempt waits up to 20 seconds.
- `KYC_REGISTERED_NEW_AMC` is missing on new-folio requests.
- Some `FAIL` results lack evidence.
- Reply wording needs polish.

## Not built

- Human review screen (S9 is `DESIGNED_NOT_BUILT`).
- Persistent audit storage.
