# TCC Desk Agent

An agent that helps a transfer-agency desk interpret incoming investor emails against the records and policy documents it holds, and returns a decision with evidence.

It is deployed at https://tcc-desk-agent.onrender.com.

## Prepare the data

```
python -m src.data
```

This builds the store: 83 folios, 522 transactions, 105 bank mandates, 95 KRA rows and 32 distributor rows (24 unique ARNs).

## Run

```
python -m src.solution
```

The app reads `PORT`.

## Environment variables

- `GEMINI_API_KEY` - from the host, never committed.
- `GROQ_API_KEY` - from the host, never committed.
- `LLM_MODE` - `auto` uses the models; `off` skips them.

## Model order

1. Gemini 3.1 Flash Lite
2. Gemini 3.5 Flash Lite
3. Groq Qwen3.8-27B
4. Groq gpt-oss-120b

## Deploy on Render

- Build: `pip install -r requirements.txt`
- Start: `uvicorn src.solution:app --host 0.0.0.0 --port $PORT`

## Where the rules live

- `src/checks.py`
- `src/outcome.py`
- `src/data.py` (`select_policy`)

## Run the evaluation

```
python evaluation/runner.py --base-url <url>
```

## Extra response fields

- `draft_status`
- `review_flags`
- `decision_basis`
- `notes`

## More

- [FUTURE_WORKFLOW.md](FUTURE_WORKFLOW.md)
- [APPROACH.md](APPROACH.md)
- [ARTEFACT.md](ARTEFACT.md)
