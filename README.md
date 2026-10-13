# TCC Desk Agent

Service that answers distributor/agent email requests about folios, transactions, KYC and mandates, using prepared records plus the governing policy documents.

Deployed at: https://tcc-desk-agent.onrender.com

## Prepare the data

```
python -m src.data
```

This reads the source files, fixes the problems found in them (see below) and writes the prepared records. After preparation the store holds 83 folios, 522 transactions, 105 bank mandates, 95 KRA rows and 32 distributor rows (24 unique ARNs).

Data problems handled during preparation:

- 56 transaction folio numbers not in `NNNNNNNN/NN` form
- 53 dates written `dd-mm-yyyy`
- 4 duplicate transaction pairs sharing a UTR
- 6 KRA rows with blank status text (status taken from the code) and 8 codes missing leading zeros
- one PAN on several folios and names written differently

## Run the service

```
python -m src.solution
```

The app reads the `PORT` environment variable and serves the HTTP API on that port.

## Environment variables

| variable | purpose |
|---|---|
| `GEMINI_API_KEY` | Gemini API key (LLM calls) |
| `GROQ_API_KEY` | Groq API key |
| `LLM_MODE` | Selects how the model is used |

## Where the rules live

- `src/checks.py` — the checks run against the records.
- `src/outcome.py` — how the outcome and reason codes are decided.
- `src/data.py`, function `select_policy` — chooses the governing policy document (SOP v4.1 current, effective 1 Aug 2026; TMF-ADD-02 effective 15 Sep 2026, TMF only; SOP v3.2 superseded and never selected). Selection reads each document's own status and effective date against `received_at`.

## Response fields

Responses carry the outcome plus these extra fields:

- `draft_status`
- `review_flags`
- `decision_basis`
- `notes`

## Other documents

- `FUTURE_WORKFLOW.md` — how a request travels through the system.
- `APPROACH.md` — approach and links.
- `ARTEFACT.md` — artefact notes.
