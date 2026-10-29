# E-state

An AI assistant that Tashkent realtors buy to answer their rental clients on
Telegram and Instagram. Each realtor gets an isolated instance with their own
listings; the agent recommends matching places, never offers a taken one,
never asks for money, never reveals the owner, and hands the client over to the
realtor for viewings.

CS363 Software Project Management, team **F1**. Requirement IDs below are from
the SRD v2.0 (Week 2).

## What runs today

| Module | Requirements |
|---|---|
| `estate/kb.py` – per-realtor knowledge base, control-bot "L-003 taken / bo'sh L-003" | F-05, F-06, N-02 |
| `estate/ingest.py` – parse a realtor's Telegram/Instagram post into a Listing | F-01 |
| `estate/extract.py` – pull budget, district and rooms out of a client's free text | F-03 |
| `estate/matcher.py` – up to 3 available matches, asks for missing budget/district/rooms | F-03, F-06 |
| `estate/language.py` – Uzbek Latin / Uzbek Cyrillic / Russian detection | F-08 |
| `estate/replies.py` – listing cards, handover, no-payment answer | F-02, F-04, F-07, F-11, F-12 |
| `estate/checker.py` – independent checker that blocks bad replies | F-10, F-06, F-11, F-12 |
| `estate/agent.py` – one turn of UC-1, falls back to handover if blocked | UC-1 |
| `estate/instances.py` – admin creates a realtor instance with its own isolated knowledge base | F-14, N-02, N-03 |
| `estate/admin.py` – per-instance stats: clients served, avg conversation time, handovers, closed deals | F-15 |

```bash
python -m estate.cli chat "2 xonali Yunusobodda 600$ gacha" --budget 600 --district Yunusobod --rooms 2
python -m estate.cli chat "Нужна квартира в Чиланзаре" --district Chilonzor
python -m estate.cli control "L-003 taken"
python -m unittest -v
```

Pure standard-library Python 3.11+, so it runs anywhere without installs.

## Not built yet (tracked in Jira project KAN)

- The actual Telegram Bot API / Instagram Messaging API wire connectors that call
  `ingest.parse_post` on a new post within 5 minutes of publication, and that let
  an admin-created instance (`estate/instances.py`) receive messages for real (N-03, F-01 live source, KAN-21)
- LLM-based understanding of free-text requests + RAG over posts (F-02)
- Wiring `estate/admin.py`'s stats log into the live agent so conversations record themselves
- Deferred to backlog: voice (F-09), publishing posts (F-13)

## How we work: Jira <-> GitHub

- Every GitHub issue, branch, commit and PR carries its Jira key, e.g.
  `KAN-14`, so the GitHub for Jira app shows the activity on the ticket.
- Branch names: `KAN-14-short-description`.
- Issues labelled `agent` are assigned to the coding agent, which opens a PR
  with `Closes #N`; a teammate submits a review before merge.
- When a PR merges, `.github/workflows/jira-sync.yml` moves its Jira ticket to **Done**.
- CI (`.github/workflows/ci.yml`) runs the test suite on every push and PR.
