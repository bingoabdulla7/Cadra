# Approach

## A. What the system does

The service takes a distributor/agent email request and answers it from prepared records: folios, transactions, bank mandates, KRA rows and distributor rows, judged against the governing policy documents. It returns an outcome plus `draft_status`, `review_flags`, `decision_basis` and `notes`. It is deployed at https://tcc-desk-agent.onrender.com. See [README.md](README.md) to run it.

## B. How a request travels

Intake reads the email and attachments; extraction pulls out the facts (rules first, then one guarded model call); identity finds the folio, investor and transaction; policy selection picks the document; checks run; the outcome and reply are decided; the trace is recorded; a person reviews where needed. The full picture, failure paths, the checks table and the people table are in [FUTURE_WORKFLOW.md](FUTURE_WORKFLOW.md).

## C. Where the rules live

Deterministic rules carry the logic, so most answers are reproducible:

- `src/checks.py` — the checks, including folio format, date parsing, duplicate UTRs, KRA status and codes, PAN/name consistency, payment confirmation and bank mandates.
- `src/outcome.py` — outcome, reason codes and reply drafting.
- `src/data.py`, function `select_policy` — selects the governing document from its own status and effective date against `received_at` (SOP v4.1 current; TMF-ADD-02 for TMF; SOP v3.2 never selected).
- `src/intake.py`, `src/llm.py`, `src/solution.py` — intake, the guarded model call, and the service entry point. All code is under [src/](src/).

## D. Evaluation

65 cases were written in this session. First accuracy measured on the live service was 55.4% (36 of 65), with 5 unsafe errors and 23 over-cautious errors. Many cases use folios with no pending register row, so the service answers `PEND` because it cannot confirm the payment; some labels were unrealistic; and the set was written by the same agent that wrote the service. Median cost per request is about $0.0002 with Gemini flash-lite (list prices, unconfirmed) and median latency about 5 seconds. Cases and notes are in [evaluation/](evaluation/).

## E. Known problems and gaps

Groq calls return HTTP 403 on Render and a model attempt waits up to 20 seconds; `KYC_REGISTERED_NEW_AMC` is missing on new-folio requests; some `FAIL` results lack evidence; reply wording needs polish. The human review screen and persistent audit storage are not built. Details are in [ARTEFACT.md](ARTEFACT.md).
