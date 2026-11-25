# Ledgerlight backend

One backend for the master platform and all eight brands: SiteSentinel, SponsorWatch, EstateSignal, DistressRadar, TenantGuard, TalentRelease, CreditPulse and FilingPulse.

Every public notice is collected **once**, scored and explained **once**, and then routed to every brand and customer it matters to. Each brand differs in what it watches, how it scores relevance, which extra fields it shows and how strict its compliance rules are. All of that is defined in `app/brands.py` and `app/fields.py`.

The API is **in-house only**. It serves the nine websites' sign-up, dashboard and admin pages. There are no customer API keys. Every call needs a signed-in user, and CORS only allows your own brand domains. Customers buy the **feed**: alerts, digests, CSV exports, file drops and managed CRM sync.

## How it fits together

```
 Companies House stream ─┐
 Companies House REST  ──┤                 ┌─ score (0–100, with reasons)
 The Gazette notices   ──┤                 ├─ director network / phoenix check
 Home Office sponsors  ──┼─► events ─► pipeline ─┼─ AI brief (Claude, or a template)
 HMLR company land     ──┤   (deduped)     ├─ compliance (suppression, vetting, cooling-off)
 Unclaimed estates     ──┤                 └─ route ─► brands ─► each customer's rules
 County court judgments ─┘                                   │
                                     ┌───────────────────────┼─────────────────────────┐
                               instant alerts          daily digest + CSV        file drops / HubSpot
                          (email, Slack, Teams, SMS)        (email)               (SFTP, Pro plan)
```

| Folder | What's in it |
|---|---|
| `app/sources/` | Collectors: `companies_house.py` (stream and REST, rate-limited to 550 calls per 5 minutes), `gazette.py` (notice parsing), `registers.py` (sponsor register, CCOD, unclaimed estates, CCJs), `risk_watch.py` (wider-risk facts and change detection) |
| `app/pipeline/` | `score.py`, `brief.py`, `compliance.py`, `route.py`, `run.py` |
| `app/delivery/` | `senders.py` (email, Slack, Teams, Twilio SMS, SFTP, HubSpot) and `dispatch.py` (instant alerts, digests, bulk exports, retention) |
| `app/brands.py` | The 9 brands: what each watches and how it scores relevance |
| `app/fields.py` | Every extractable field, its level (Simple, Standard, Advanced, Full record) and its brand |
| `app/plans.py` | Solo £29, Team £99, Pro £299, Enterprise. Annual is 10× monthly. Includes the entitlements for each plan |
| `app/web.py` | The in-house HTTP API (Starlette) |
| `app/worker.py` | The scheduler that runs every job |
| `web/ledgerlight-client.js` | Drop-in script that connects the websites to the API |
| `data/risk_factors.json` | The 48 wider-risk facts shown on the brand sites (6 per brand) |

## Run it locally (about 2 minutes)

```bash
pip install -r requirements.txt
cp .env.example .env            # all keys can stay blank for local testing
python -m scripts.seed_demo      # sample companies, notices and one demo login per brand
uvicorn app.web:create_app --factory --reload
python -m app.worker             # in a second terminal
python -m pytest -q              # 54 tests
```

The default database is SQLite. For production, set `DATABASE_URL=postgresql://…` or use `docker compose up`.

While `LL_DRY_RUN_DELIVERY=true`, every email, Slack message, SMS, SFTP file and HubSpot update is written to `./outbox/` instead of being sent.

## Cloud (Render)

`render.yaml` is a Render Blueprint: Dashboard → New → Blueprint → this repository. It creates
`ledgerlight-api` (web: portal, admin, outreach console, API), `ledgerlight-worker` and the `ledgerlight-db`
Postgres database in Frankfurt. Before each deploy goes live, `scripts/predeploy.py` runs the full test suite
against the real Postgres database (in a throw-away `ll_selftest` schema) and then creates/upgrades the tables;
if anything fails, the previous version keeps running.

Secrets (Companies House keys, Gmail app password, etc.) are typed into Render when the Blueprint is created,
never committed. `LL_DRY_RUN_DELIVERY` starts as `true` (nothing is emailed); set it to `false` to go live.

Moving local data: stop the local worker, then double-click `migrate-to-cloud.command` and paste the
database's External Database URL. Safe to re-run. Only ever run one worker (cloud OR laptop), never both.

## Market terminal (`/terminal`)

A dark, terminal-style screen for customers: one live wire of UK market news and Ledgerlight signals, linked to
company risk. Left: sector monitor (signals this week vs last, news volume and negative share) and watchlist movers.
Centre: the wire (amber = Ledgerlight signal from an official source, white = news; tone dot; tags for watchlist,
your sector, high-risk company, insolvency, rates, opportunity). Right: the selected item's linked companies with
risk bands, a point-of-view selector and **What this means to you**. Key, keyboard shortcuts (`/`, `j`, `k`, Enter)
and a "My business" profile (role, sector, region, notes) that tailors the analysis.

- **News (`app/sources/market_news.py`):** GDELT sector queries in rotation every 15 minutes (`market_news` job),
  whole-word theme tagging (rates, insolvency, costs, jobs, demand, deals, regulation, contracts), tone, and exact
  company-name links to tracked companies. Only headline, link, domain and company numbers are stored.
- **Agents (`app/terminal.py`):** Researcher gathers Ledgerlight facts (linked companies' bands and reasons, the
  customer's profile and watchlist exposure in the same sectors); Analyst explains from one perspective (my company,
  customers, suppliers/contractors, lenders/landlords, opportunity) using Claude when `ANTHROPIC_API_KEY` is set,
  otherwise a rules engine; Reviewer keeps it to the facts, ranks actions by urgency and adds the disclaimer.
  Answers are cached for 6 hours per item and perspective; each new answer uses one of the plan's `briefs_per_month`.
- **Rules kept:** signals come through `views.visible_events`; personal-data notices are never shown, explained or
  sent to the model; objections hide linked companies.
- **API:** `GET /v1/terminal/feed`, `/v1/terminal/sectors`, `/v1/terminal/movers`, `POST /v1/terminal/explain`,
  `GET|PUT /v1/terminal/profile`.

## Profit playbooks

Specific ways each industry turns the data into money, with a step-by-step guide that uses the portal and the terminal
(3 plays per brand, 24 in all). One source: `data/playbooks.json`.

- **Portal:** the *Profit playbooks* tab (`/app?play=<id>` deep-links one play); the org's own industry comes first.
- **Terminal:** every "What this means to you" answer links to the matching plays (`app/playbooks.suggest`).
- **API:** `GET /v1/playbooks` and `GET /v1/playbooks?id=<id>` (signed-in users only).
- **Websites:** all nine sites carry a Playbooks, Platform and Terminal-preview block built from the same file by
  `python -m scripts.build_site_blocks` (output in `build/site_blocks/`, not committed). The `APP` URL at the top of
  that script is where the sites' "Open the portal / terminal" buttons point. Re-apply the blocks when plays change.

## Company Insight (risk model)

Every tracked company gets a 12-month probability of formal insolvency, a band (Low / Watch / Elevated / High /
Critical), its top three reasons in plain words, trend vs last month, sector percentile and the recommended action
for the customer's product. Design and evidence: the "Ledgerlight Company Insight" doc.

- **Model (`app/insight.py`):** sector base rate × company-age adjustment as the prior. Both come from the
  Insolvency Service's *Business insolvency demography* (2025 figures, per 10,000 active businesses) in
  `data/base_rates.json`; refresh that file each June when the next release is out. Staff overrides (table
  `base_rates`, admin → Risk model) win over the official figure. Then behavioural/event signals add log-odds (late filing, director exits, CCJs, petitions,
  charges under stress, ownership/office changes, sponsor licence loss, net assets, negative news). Formal insolvency
  events override the model. Personal-data notices are never inputs; objections hide a company entirely.
- **Learning loop:** `insight_snapshot` (daily) writes monthly snapshots; `insight_label` marks failures within 12
  months; `insight_fit` (weekly) fits a discrete-time hazard model once 200 failures are labelled, replacing the
  starting weights. Admin → Risk model shows calibration (predicted vs actual per band) and AUC.
- **News (`app/sources/news.py`):** GDELT DOC 2.0 for watched companies (`insight_news`, hourly); headlines with
  warning terms nudge the score a little and are shown with their links. No private individuals' names are stored.
- **API:** `GET /v1/insight/company/{number}`, `GET /v1/insight/watchlist`, `POST /v1/insight/company/{number}/decisions`,
  `PATCH /v1/insight/decisions/{id}`, `GET /v1/insight/decisions`; admin `GET /v1/admin/insight`,
  `POST /v1/admin/insight/base-rates`. Lookups count against `insight_lookups_month` (Solo 25, Team 250, Pro 2,500,
  Enterprise unlimited); watchlist companies are free. Raising exposure to a High/Critical company needs a second
  reviewer from the same account.

## Customer portal and admin dashboard

- **Customer portal: `/app`** (e.g. `http://localhost:8000/app`). Customers sign in or start a 14-day trial, then build an extract:
  choose records (event type, region, dates, company, risk score, watchlist), choose a detail level and columns, preview 25 rows and
  download CSV. Everything is enforced on the server by plan: locked levels/fields return 402, and each plan has a per-file and
  monthly row allowance (`rows_per_export`, `export_rows_month` in `app/plans.py`). Brand sites can link to `/app?brand=sitesentinel&signup=1`.
- **Admin dashboard: `/admin`** (staff accounts only, see `scripts/make_staff.py`). Overview KPIs and charts, data-source/job health,
  customers (change plan/status, vet, extend trial, reset a password), every event collected, review queues (wider-risk facts,
  suppressions), run jobs now, and the audit log.

## Outreach: selling our products by email

**Quickest start (Mac):** double-click `outreach-test-setup.command`. It installs everything, asks for your Gmail app password and test addresses, sends a test email to you, and opens the console.

Open `/outreach` on the API (for example `http://localhost:8000/outreach`) and sign in as staff. It works like the Google Sheet + Streak + Gmail routine, without the extra tools:

1. **Mailboxes.** Add each Gmail or Workspace mailbox you'll send from (preset: `smtp.gmail.com:587`, `imap.gmail.com:993`). Passwords never go in the console. Create a Google **app password** for the mailbox and put it in `.env` under the name you chose, e.g. `OUTREACH_PW_SAM=...`.
2. **Import leads.** Upload or paste a CSV with the same columns as the sheet: Name, Position, Email, Website, Date Found (Company and Company Number optional). Use **Find prospects** to download companies in our database that look like buyers of a product, then research their contacts.
3. **Campaigns.** Each product starts with a 3-step sequence (day 0, 3 and 7). Edit it, preview it per lead, add leads and press **Start sending**.
4. **Pipeline.** Leads move through new, contacted, replied, interested, trial, customer. A reply stops the sequence and moves the lead to Replied. Bounces and "unsubscribe" replies suppress the address everywhere.

**Built-in safeguards:**
- Daily cap per mailbox (40 by default) with warm-up: 10 a day at first, plus 3 each day after.
- Sending only Monday to Friday, 9:00 to 17:00 UK time.
- A random 90–240 second gap between emails.
- Follow-ups sent as replies in the same thread.
- One-click unsubscribe headers, and a footer with your company name, address and an unsubscribe link.
- Personal mailboxes (Gmail, Hotmail…) and sole traders are blocked: the law (PECR) requires consent for those.
- Suppressed, bounced and unsubscribed addresses are never emailed again.
- Sending won't start for real until `OUTREACH_POSTAL_ADDRESS` and each mailbox password are set.

**For good deliverability:**
- Send from a separate domain, not your main one.
- Set up SPF, DKIM and DMARC for that domain.
- Keep each mailbox at 30–50 emails a day, and add mailboxes rather than raising caps.
- Pause if bounces go over 3%.

## Jobs (app/worker.py)

| Job | How often | What it does |
|---|---|---|
| CH streams | continuous | Company profile, insolvency, charges, officers and PSC changes, picked up from the last saved timepoint |
| `gazette` | 5 min | New insolvency, strike-off and deceased-estate notices |
| `process` | 30 s | Score, brief, check compliance and route new events |
| `instant` | 1 min | Send instant alerts, retrying up to 5 times |
| `digests` | 10 min | One digest per customer per day at their chosen hour, with a CSV attached |
| `exports` | 1 min | Bulk CSV export jobs |
| `sponsors` | daily | Compare the Home Office register with yesterday's: sponsors added, removed and re-rated |
| `unclaimed_estates` | daily | Compare the Government Legal Department's unclaimed estates list |
| `ccjs` | hourly | County court judgments from a licensed provider |
| `ccod` | monthly | Land Registry company ownership data, flagging land held by dissolved companies |
| `enrich_watched` | daily | Refresh every company on any customer's watchlist |
| `risk_sources` | weekly | Re-check the 48 wider-risk sources and flag changed ones for a person to review |
| `expire_trials`, `purge` | hourly / daily | End unpaid trials; apply data retention |
| `outreach_send` | 1 min | Send due outreach emails within caps and sending windows |
| `outreach_inbox` | 5 min | Read each mailbox (IMAP): replies, bounces, unsubscribes |

Staff can run any job on demand with `POST /v1/admin/run/{job}`.

## API (in-house)

**Public:**
- `GET /health`
- `GET /v1/plans`
- `GET /v1/risks?brand=`
- `GET /v1/exports/fields.csv?brand=`
- `POST /v1/privacy/object`: the right to object, applied across all brands

**Account:**
- `POST /v1/auth/signup` (starts a 14-day trial with Team features)
- `POST /v1/auth/login`
- `GET /v1/me`
- `PATCH /v1/org`
- `POST /v1/org/members`

**Feed:**
- `GET /v1/events?type=&region=&min_score=&since=&q=&watchlist=1`
- `GET /v1/events/{id}`
- `GET /v1/companies/{number}`

**Watchlists:**
- `GET|POST /v1/watchlists`
- `GET|DELETE /v1/watchlists/{id}`
- `POST /v1/watchlists/{id}/items`
- `POST /v1/watchlists/{id}/import`: CSV upload, column names are flexible
- `GET /v1/watchlists/{id}/export.csv`

**Alerts:**
- `GET|POST /v1/alert-rules`
- `PUT|DELETE /v1/alert-rules/{id}`
- `GET|POST /v1/channels`: email, Slack, Teams, SMS, SFTP or HubSpot, limited by plan

**Exports:**
- `GET /v1/exports/events.csv`
- `POST /v1/exports/bulk`
- `GET /v1/exports/bulk/{id}[/download]`

**Billing:**
- `POST /v1/billing/checkout {plan, cycle}`
- `GET /v1/billing/portal`
- `POST /v1/billing/webhook`: Stripe, with verified signatures

**Admin (staff only; grant with `python -m scripts.make_staff EMAIL`):**
- `GET /v1/admin/stats`
- `GET /v1/admin/orgs`
- `PATCH /v1/admin/orgs/{id}`: set `vetted`, `plan` or `status`
- `GET|PATCH /v1/admin/risk-factors`
- `GET|POST /v1/admin/suppressions`
- `POST /v1/admin/run/{job}`

Every list and every CSV goes through `app/views.py`. The same brand, vetting, cooling-off and plan-level rules therefore apply in the dashboard, the alerts and the exports.

## Compliance built in

- **Personal data is minimised at intake.** Deceased-estate notices keep only the claims deadline, the executor firm and the postcode district. Names and addresses are dropped, and no personal details are ever sent to the AI model.
- **EstateSignal events need staff vetting.** They only reach an organisation after staff set `vetted=true`, and they are held back for a cooling-off period (`LL_PROBATE_COOLING_OFF_DAYS`, 14 by default).
- **Objections and suppressions apply to every brand.** They are checked before an event is routed.
- **Sole traders and partnerships are flagged.** Each company gets a `subscriber_type` so customers follow the stricter PECR consent rules for those.
- **Personal insolvency is never routed.** Bankruptcies, IVAs and DROs are not turned into leads.
- **Old data is purged.** Personal-data events and old deliveries are removed after a retention window.
- **Rules are checked twice.** Plan status, vetting, cooling-off and suppression are checked when an alert is queued, and again when it is sent. An objection or a cancelled subscription therefore stops alerts that were already queued.

## Security

- **Staff access is set on the server only.** Run `python -m scripts.make_staff EMAIL`. It is never inferred from an email address.
- **Session tokens are checked against the account.** The organisation in a token must match the user's own.
- **The service refuses a weak secret.** Outside development it won't start without a `LL_SECRET_KEY` of at least 32 characters.
- **Logins are rate-limited by real client IP.** `X-Forwarded-For` is only trusted when `LL_TRUST_PROXY=true`, and then only the address your proxy appended.
- **Every trial gets exactly Team features.** Enterprise can't be chosen at sign-up.
- **Stale Stripe events are ignored.** That covers events for an old subscription and events that arrive out of order.
- **Postgres connections recover from errors.** They run in autocommit mode and roll back on failure, so one failed statement can't take down the API.

An independent code review found 14 defects, which are all fixed. Each one has a regression test in `tests/test_review_fixes.py`.

## Before going live

1. **Get API keys.** Companies House REST and stream keys are free. Set up Stripe products and prices for the six price IDs, an SMTP provider and optionally Twilio and Anthropic.
2. **Check The Gazette feed format and notice codes.** Compare `gazette.FEED_URL` and `NOTICE_CODES` against The Gazette's current data documentation. The parser falls back to classifying notices by title, but the codes should be confirmed.
3. **Licences.** CCJ data needs a commercial licence (Registry Trust). Check the CCOD terms before showing title numbers. Gazette personal data needs your own UK GDPR legitimate-interests assessment.
4. **Host the 9 websites on your own domains.** Add `web/ledgerlight-client.js` and list the domains in `LL_ALLOWED_ORIGINS`. Pages hosted on claude.ai can't call outside APIs.
5. **Turn delivery on.** Set `LL_DRY_RUN_DELIVERY=false` only after test sends look right.
6. **Get a data-protection review.** Have a lawyer check the legitimate-interests assessments, privacy notices and the EstateSignal code of conduct.
