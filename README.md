# adsum-gateway

Part of the ADSUM platform (membership, QR check-in and attendance).
Subgroup: `services`.

## Role

The single outbound door of the platform: email, instant messaging, SMS and push.
Nothing else in ADSUM calls an external messaging provider directly.

It exists to fix one concrete defect. ADSUM's own dunning email used to leave through
a *client organisation's* sending chain: a parish had its own sending quota consumed
by its supplier's invoicing, and a payment reminder arrived signed with the name of
the person receiving it. This service carries the editor's identity: its domain, its
validated sender, its own register.

## What it guarantees

- **A message sent twice is sent once.** The idempotency key is reserved in the
  database *before* the provider is called. Two concurrent calls race on the insert;
  one sends, the other returns the first result.
- **A blocked address receives nothing.** Unsubscribes and dead addresses are kept,
  per channel, and checked before every send. Writing to a dead address damages the
  domain's sending reputation, which eventually sends everyone else's mail to spam.
- **One provider down does not silence the platform.** Providers are ranked per
  channel; a failure moves to the next one. A definitive refusal stops the chain,
  because retrying elsewhere only multiplies complaints against the domain.
- **No address is ever stored in clear.** The register keeps a peppered HMAC of the
  normalised address, never the address. It answers "did we write to this address"
  without becoming an address book if the database is ever copied elsewhere. The
  pepper lives outside the database, which is what makes a stolen copy useless.

## Stack

Python 3.11, FastAPI, httpx, PostgreSQL.

## Providers

| Channel  | Provider | Notes |
|----------|----------|-------|
| courriel | Brevo    | Editor's validated sender. Delivery receipts authenticated by a secret carried in the receiving address, since Brevo does not sign. |
| telegram | Telegram bot | Plain text on purpose: one unescaped character in MarkdownV2 drops the whole message. |
| sms      | none yet | Declared in the contract, no adapter. |
| poussee  | none yet | Declared in the contract, no adapter. |

## Configuration

| Variable | Required | Purpose |
|----------|----------|---------|
| `ADSUM_PASSERELLE_DSN` | yes | PostgreSQL connection for the register. |
| `ADSUM_PASSERELLE_SECRET` | yes | Shared secret the calling services must present. At least 32 characters; the service refuses to start below that. |
| `ADSUM_PASSERELLE_POIVRE` | yes | Pepper for the address digest, at least 32 characters. Kept **outside** the database. |
| `ADSUM_PASSERELLE_APPELANTS` | recommended | One secret per caller, `name:secret` separated by commas. The register then records who asked for each send. |
| `ADSUM_BREVO_CLE` | for email | Brevo API key. |
| `ADSUM_BREVO_EXPEDITEUR` | for email | Sending address, **validated at Brevo**. An unvalidated address is accepted by the API then silently dropped at delivery. |
| `ADSUM_BREVO_NOM` | no | Display name, defaults to `ADSUM`. |
| `ADSUM_BREVO_SECRET_ACCUSE` | for receipts | Without it, delivery receipts cannot be authenticated and are refused. |
| `ADSUM_TELEGRAM_JETON` | for Telegram | Bot token. |

The service refuses to start without the first two: a gateway running with an empty
call secret accepts any send request, and the fault only shows once messages have
gone out in our name.

The pepper deserves its own paragraph. A bare SHA-256 of an email address protects
nothing: the space of plausible addresses is small, so anyone holding a copy of the
table recovers them by dictionary, hundreds of thousands of guesses a second on an
ordinary machine. With a pepper held outside the database, a stolen copy of the
database alone yields nothing, while the service still recognises an address
submitted to it, which is the only thing it needs. Digests carry a `v1:` prefix so
the pepper can be rotated without the old values becoming values nobody can
explain.

## Database

Apply the migrations with `deployment/database/creer_schema_passerelle.py`, never by
hand. It creates the `passerelle` schema and sets the search path **before** the
first migration runs. Applied with the default search path, the tables land in
`public`, which the hosting exposes through an automatic API reachable by an
anonymous role: the editor's whole send register would be readable with no
authentication at all. The script also checks afterwards that nothing landed
outside the intended schema, and rolls everything back if it did.

## Endpoints

- `GET  /health` - open. Channels served, providers, configuration anomalies.
- `POST /api/v1/envois` - send a message. Idempotent on `cle_idempotence`.
- `GET  /api/v1/envois/{cle}` - state of one send.
- `GET  /api/v1/envois` - recent feed, for operations. Never an address.
- `GET  /api/v1/indicateurs` - per-channel counters, so a degradation is seen before
  a client reports it.
- `DELETE /api/v1/blocages/{canal}` - lift an address block. A corrected address
  exists, and a block with no way out would condemn an organisation to silence.
- `POST /api/v1/accuses/{fournisseur}` - provider delivery receipts. No call secret:
  these come from the internet and prove their origin by their signature.
- `POST /api/v1/taches/purge` - apply the retention periods. Triggered daily by
  `adsum-workers`. A retention period nobody triggers is not a retention period.

## One secret per caller

With a single shared secret, the register never knows which service asked for a
send. A compromised service can write to any address under the editor's identity and
nothing tells you which one it was, nor can you revoke it without cutting every other
service at the same time.

`ADSUM_PASSERELLE_APPELANTS` takes `commerce:<secret>,ouvriers:<secret>`. Each send
records the caller name, deduced from the secret presented and never from the request
body: a value the caller picks itself proves nothing. Secrets shorter than 32
characters are skipped with a warning rather than accepted silently, because that
variable is edited by hand.

The historic single secret keeps working, recorded as `inconnu`. Removing it the day
you introduce named secrets would cut the services already in place, and the outage
would show up as reminders that stop going out, with no message saying why.

## Retention

Thirteen months for sends, ninety days for attempts. Sends answer "did you write to
this client", a question that still comes up an accounting year later; attempts only
help understand an outage in progress, and nobody reads them after a few weeks.

Blocked addresses are never purged. An unsubscribe is permanent, and forgetting it
would restart messages to somebody who asked to stop receiving them.

One migration, `migrations/versions/0001_journal_envois.sql`. Four tables: `envoi`,
`adresse_bloquee`, `accuse`, `tentative`. The migration names no schema on purpose:
hard-coding one would stop it being applied to a test schema or a second
environment. It does close access to whichever schema it lands in, with `REVOKE` and
row level security forced on all four tables.

## Tests

```
python -m pytest tests/ -q
```

Real HTTP application, real database, observed providers. Sending real email from a
test suite would fill real inboxes and damage the sending reputation, which is
exactly the harm this service exists to prevent.

## Deployment status

Not deployed. Requires, from the owner: a Brevo account for the editor, its own
sending domain with SPF, DKIM and DMARC published, and a decision on where the
register lives.
