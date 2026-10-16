# Realtor Agent

An AI realtor assistant for the Tashkent market. It:

- **matches buyers to listings** by budget, district, rooms and must-haves
- **answers questions about a property** from the listing data (no made-up facts)
- **books viewings** into free slots and refuses double-bookings

The core is plain Python (stdlib only), so it runs and tests anywhere.
An optional LLM layer (`realtor_agent/llm.py`) turns a free-text request
("2-room flat in Yunusobod under $80k, near a metro") into a structured search.

## Quick start

```bash
python -m realtor_agent.cli search --budget 80000 --district Yunusobod --rooms 2
python -m realtor_agent.cli ask L-003 "Is there parking?"
python -m realtor_agent.cli book L-003 "Aziz" 2026-10-20T15:00
python -m unittest -v
```

## Project management

Work is tracked in **Jira** and mirrored on **GitHub**:

- Every Jira ticket key (e.g. `RA-12`) goes into the branch name, the commit
  messages and the PR title, so the GitHub for Jira app links the activity.
- A merged PR moves its Jira ticket to **Done** (see `.github/workflows/jira-sync.yml`).
- Issues labelled `agent` are picked up by the coding agent, which opens a PR
  that says `Closes #N`; a human reviews it before merge.

## Layout

| Path | What |
|---|---|
| `realtor_agent/listings.py` | Listing model + loading from `data/listings.json` |
| `realtor_agent/matcher.py` | Scores and ranks listings for a buyer |
| `realtor_agent/qa.py` | Answers property questions strictly from listing fields |
| `realtor_agent/booking.py` | Viewing calendar, conflict checks |
| `realtor_agent/llm.py` | Optional: free text -> search criteria (Claude API) |
| `realtor_agent/cli.py` | Command-line entry point |
| `tests/` | `unittest` suite, run in CI on every push/PR |
