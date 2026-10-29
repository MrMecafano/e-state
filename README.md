# E-state

A tenant-isolated rental assistant for Tashkent realtors. Each instance answers
Telegram/Instagram rental enquiries from its own listings, recommends at most
three available matches, and hands viewing/renting requests to the realtor.

CS363 Software Project Management, team **F1**. Requirement IDs refer to SRD v2.0.

## Implemented code

| Capability | Modules | Requirements |
|---|---|---|
| Source-post ingestion, complete listing facts and persistent availability | `ingest`, `kb`, `storage` | F-01, F-05, F-06 |
| Tenant-scoped listings/history, restart persistence and control updates | `instances`, `storage` | N-02 |
| Official Telegram Bot API and Instagram Messaging API adapters | `connectors` | F-02, N-03 |
| Stateful criteria extraction and up to three matching cards | `extract`, `matcher`, `service` | F-02, F-03 |
| Individual listing photos/videos, signed cross-platform file delivery | `connectors`, `media`, `service` | F-04 |
| Uzbek Latin/Cyrillic and Russian controlled replies/cards | `language`, `replies` | F-08 |
| Independent privacy/payment/availability checking and checked fallback | `checker`, `agent`, `service` | F-06, F-07, F-10, F-11, F-12 |
| Optional local whisper.cpp voice transcription | `voice`, `service` | F-09 |
| Authorised Telegram `/publish` command | `connectors`, `service` | F-13 |
| Authenticated instance creation, statistics and explicit deal recording | `server`, `admin_panel.html`, `admin` | F-14, F-15 |
| ACK-boundary first-reply measurements and offline 100-message benchmark | `telemetry`, `tools/benchmark_latency.py` | N-01 code instrumentation |

Core runtime/tests use standard-library Python **3.11+**; no provider SDK or LLM
key is required (AI replies are optional, see below). Free-text extraction is deterministic, not a claim of LLM/RAG
coverage for arbitrary wording. Controlled labels are translated; source titles
and place spellings are not fabricated translations.

## Offline verification

```sh
python -m unittest discover -s tests -t . -v
python tools/benchmark_latency.py --messages 100
python -m estate.cli chat "2 xonali Yunusobodda 600$ gacha"
python -m estate.cli control "L-003 band"
```

`chat` reads budget, district and rooms from the message; `--budget`,
`--district` and `--rooms` only override what it found.

Connector tests inject HTTP transports, voice tests inject a local process runner,
and application tests use synthetic data. They do **not** contact Telegram,
Instagram, Jira or a transcription provider. The benchmark actually processes
100 distinct synthetic messages but measures **local reply preparation**, not
platform delivery, end-to-end API latency or 24/7 availability. A successful send
is recorded by the runtime only after the connector returns a platform ACK.

## Laptop demo bot (real Telegram chat, no server)

`estate.polling` runs one demo realtor's bot on a laptop with Telegram long
polling, so no public URL or webhook is needed. It is a demo runner, not the
deployment path, and it starts fresh from `data/listings.json` in memory on
every run (nothing is written to disk).

1. In Telegram, message **@BotFather**, send `/newbot`, and copy the token.
2. `cp .env.example .env` and fill in the four `ESTATE_DEMO_*` lines. `.env` is
   git-ignored; never commit or share the token.
3. Run `python3 -m estate.polling` from the repository root and open the
   `t.me` link it prints.

The realtor and the client must be **different Telegram accounts**: messages
from `ESTATE_DEMO_REALTOR_IDS` are treated as commands (`L-003 band`,
`L-003 занято`), not questions. To find the realtor's ID, start the bot with the line blank, message
it from the realtor's account, and copy the number the terminal shows.

Conversation order: the bot first asks for district, rooms and monthly budget,
then lists up to three places, and gives the realtor's contact only when the
client asks to see a place or to talk to the realtor ("show me the options" is a
search, not a viewing). Payment questions are refused without a contact.

Each reply uses one language, the client's: Uzbek (Latin or Cyrillic) or
Russian, cards and listing titles included (titles are stored as
`Uzbek / Russian`). A message with no clear language (`700$`) keeps the client's
earlier language. Only before the client has shown one (`/start`, an English
first message) does the bot answer in Uzbek and Russian together.

The demo channel: add the bot to a channel as an admin and set
`ESTATE_DEMO_CHANNEL` to its numeric ID (`-100...`; post anything in the channel
and the ID appears in the bot's updates). `python3 tools/post_demo_listings.py`
prints the demo listings as Uzbek + Russian e'lons; `--send` posts them and
saves each post link in `data/listings.json`. Telegram does not let a bot read
old channel posts, so the bot starts from that file and learns **new** posts
while it runs. A post that contains its ID (`🆔 L-007`) keeps that ID, so the
realtor can send `L-007 band`; any other post gets a long generated ID.

The demo listings' photos are local file names, which Telegram cannot send, so
those cards go out as text. To show real photos, post a new e'lon with a photo
in the channel, for example `Chilonzor, 2 xonali, 60 m2, 450$` plus `🆔 L-007`.
Only one copy of the bot can poll at a time; stop the other one if the terminal
says Telegram is unreachable.

If it says Python cannot check Telegram's security certificate, that `python3`
(often the python.org installer) has no certificates installed. Use another
Python, such as Homebrew's `python3.12 -m estate.polling`, or run
`Install Certificates.command` from that Python's folder in Applications.

## AI replies (optional)

With `ANTHROPIC_API_KEY` set, Claude writes the reply to normal rental
questions, so clients can use their own wording ("Yunusobod tomonda arzonroq
narsa bormi?"). Without it, the rule-based replies above are used.

- **What is sent:** every free listing of that realtor (id, title, district,
  rooms, area, rent, amenities, photo count, post link) plus the client's
  current conversation. Owner names and phones are never sent. There is no
  retrieval step yet; the plan is to move to RAG and send only matching listings.
- **What stays in code:** listing cards, photos and links come from the
  knowledge base, not from the model. Code picks the reply language and rejects
  a draft that lists places before district, rooms and budget are known. The
  model never sees the realtor's contact; code adds it only to a handover.
  `/start` and payment questions keep fixed replies. Every model draft passes
  the same independent checker, and a failed, slow (30 s) or rejected draft is
  replaced by the rule-based reply. The bot log says which one answered.
- **Model and cost:** `claude-opus-5-5` at low effort by default (change with
  `ESTATE_AI_MODEL`); each reply is billed to the key's Anthropic account.
  Server-side refusal fallbacks are on for models that support them.
- **Setup:** the SDK needs Python 3.10+. Homebrew's Python blocks a global
  `pip install`, so use the project's git-ignored virtual environment:

  ```sh
  python3.12 -m venv .venv
  .venv/bin/pip install anthropic
  .venv/bin/python -m estate.polling
  ```

Tests never call the API; they inject a fake client.

## Operator setup (not deployed)

All values in `.env.example` are intentionally blank. The server does not
source dotenv files (only the laptop demo bot reads `.env`). Export values through your own secret manager or
process supervisor; never commit them. Do not reuse credentials from another
project. Tenant records store credential **environment-variable names**, not
credential values.

1. Set a unique `ESTATE_ADMIN_TOKEN` with at least 24 characters.
2. For each realtor, supply its own Telegram bot/Instagram account credentials and
   webhook verification settings in the operator environment.
3. Start the reference adapter locally:

   ```sh
   python -m estate.server --database var/estate.sqlite3 --host 127.0.0.1 --port 8080
   ```

4. Open `/admin`, enter the admin token, and create an instance. Enter environment
   variable **names** in credential fields, numeric Instagram account ID, source
   Telegram channel, realtor contact and authorised Telegram control-user IDs.
   Creation means configured, **not live connected or verified**.
5. When deploying later, register official HTTPS webhook URLs:
   `/webhooks/{instance_id}/telegram` and `/webhooks/{instance_id}/instagram`.
   Configure Telegram's secret header; Meta POSTs require the raw-body app-secret
   HMAC. The Instagram subscribe challenge uses the instance verification secret.

Telegram channel aliases are explicitly resolved with official `getChat` when a
credential-configured connector is first used. The bot needs channel access and
publication privileges. Numeric channel IDs avoid alias lookups. Source posts
are processed from authenticated channel updates; no historical account scraping
or userbot is used.

### Availability and publication commands

Only configured `telegram_admin_ids` can issue control commands through Telegram:

```text
L-003 band        (or: L-003 банд, L-003 занято, L-003 taken)
bo'sh L-003       (or: L-003 бўш, L-003 свободно)
```

The reply comes back in the language of the status word. `/list` shows every listing with
its status, and `/start` shows a command menu (never a client reply).

With AI replies on, the realtor can also write plain sentences ("Minor metrodagi
uy ijaraga berildi", "Сколько клиентов писали сегодня?", "e'lon yozib ber"). The
assistant sees every listing with its status and client counts (no owner
details, client IDs or chats), answers questions and drafts e'lons. It only
*proposes* status changes: the bot asks "Ha / Yo'q" and changes nothing until
the realtor confirms within 10 minutes. Exact commands, `/list`, `/start` and the
confirmation itself never go to the model.

Publication syntax (new listing details on the next line):

```text
/publish L-004
Yunusobod, 2 xonali, 55 m2, 500$
```

Attach that property's photos/video to the command. The publication gate checks
facts, owner privacy and existing availability before using the configured
Telegram channel. Normal renters cannot acquire control privileges by sending
these words. The authenticated admin API also supports scoped listing/control
operations.

### Photos and video across platforms

Telegram file IDs are reusable only within Telegram. For Instagram delivery of
those files, configure `ESTATE_PUBLIC_BASE_URL` to your HTTPS application origin
and a unique `ESTATE_MEDIA_SIGNING_KEY` of at least 24 characters. The gateway
issues ten-minute tenant/listing-bound capabilities and resolves file bytes
server-side. It does not expose Telegram bot-token download URLs. Public HTTPS
media references must not contain credentials. Unavailable/taken listings and
expired/tampered capabilities fail closed.

### Optional voice

Install **whisper.cpp**, a suitable multilingual model, and optionally **ffmpeg**
for Telegram/Instagram audio conversion. Set `WHISPER_CPP_BIN`,
`WHISPER_MODEL_PATH`, and `FFMPEG_BIN` to operator-managed local paths. With both
whisper settings blank, users are asked to send text. Files and process execution
are size/time bounded; Instagram downloads are limited to trusted Meta CDNs.
No model is bundled and actual Uzbek/Russian transcription quality has **not**
been validated by the stubbed tests.

### Statistics and delivery semantics

The admin panel shows distinct clients (channel/client namespace), average
conversation elapsed time, handovers and **explicitly confirmed** closed deals.
A handover or 30-minute inactivity gap starts a new session. Deal closure is never
inferred from chat text. Session and latency records persist in SQLite.
Duplicate receipts are tenant/channel/client scoped. Failed sends are retryable;
partial multi-part delivery can duplicate on retry. This is **at-least-once**, not
an exactly-once delivery promise.

## Deployment acceptance still required

This repository has not been deployed or validated with live platform accounts.
N-01's live <60-second first reply and 24/7 availability remain deferred. Operators
must supply credentials, app permissions, HTTPS/reverse proxy, process supervision,
backups, health monitoring, and realistic load/live media/voice checks. The bundled
HTTP adapter is a development/reference adapter, **not** a production worker
architecture. Verify media fetching during platform ACK handling and use an
appropriate concurrent production HTTP adapter before real traffic.

Keep SQLite files/backups private: they include client history and owner facts.
Do not expose the global admin bearer token to realtor clients; it authorises the
operator across instances. Rotate tokens/keys, restrict filesystem permissions,
and set retention/access policies before deployment.

## Jira and GitHub

Branches/commits/PRs carry requirement-associated Jira keys such as `KAN-14`.
Reviews describe actual findings and verification, rather than ceremonial approval.
The CI workflow runs offline tests; local results do not prove a hosted Actions
run occurred. Jira credentials/integration are deliberately deferred. Jira sync
is disabled and additionally requires **both** `JIRA_SYNC_ENABLED=true` and a
`jira-done` label on a merged PR for a fully completed Jira task. Missing secrets
skip transitions. Partial requirements must not transition a broader shared task.
