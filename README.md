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
key is required. Free-text extraction is deterministic, not a claim of LLM/RAG
coverage for arbitrary wording. Controlled labels are translated; source titles
and place spellings are not fabricated translations.

## Offline verification

```sh
python -m unittest discover -s tests -t . -v
python tools/benchmark_latency.py --messages 100
python -m estate.cli chat "2 xonali Yunusobodda 600$ gacha" --budget 600 --district Yunusobod --rooms 2
python -m estate.cli control "L-003 taken"
```

Connector tests inject HTTP transports, voice tests inject a local process runner,
and application tests use synthetic data. They do **not** contact Telegram,
Instagram, Jira or a transcription provider. The benchmark actually processes
100 distinct synthetic messages but measures **local reply preparation**, not
platform delivery, end-to-end API latency or 24/7 availability. A successful send
is recorded by the runtime only after the connector returns a platform ACK.

## Operator setup (not deployed)

All values in `.env.example` are intentionally blank. The application does not
implicitly source dotenv files. Export values through your own secret manager or
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
L-003 taken
bo'sh L-003
```

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
