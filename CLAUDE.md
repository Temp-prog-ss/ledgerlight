# Ledgerlight backend: notes for Claude Code

One backend serves the master platform (Ledgerlight) and eight brands: sitesentinel, sponsorwatch, estatesignal, distressradar, tenantguard, talentrelease, creditpulse and filingpulse. Read README.md for the architecture, jobs and endpoints.

## Commands
- Install: `pip install -r requirements.txt`
- Tests: `python -m pytest -q`. All must pass before any commit or deploy.
- Demo data: `python -m scripts.seed_demo`
- API: `uvicorn app.web:create_app --factory --reload`
- Worker: `python -m app.worker`
- Staff access: `python -m scripts.make_staff EMAIL`
- Full stack: `docker compose up --build` (Postgres, api, worker)

## Rules that must not be broken
1. **The API is in-house only.** Never add customer API keys, public endpoints for raw data, or CORS wildcards. Customers buy the feed (alerts, digests, CSV, file drops, CRM sync), not API access.
2. **All reads go through `app/views.py`.** Every list, alert, digest and CSV goes through `visible_events`/`project_events`, so the brand, vetting, cooling-off, suppression and plan-level rules apply everywhere. Don't query events directly for customer output.
3. **Compliance is checked twice:** when an alert is routed (`pipeline/route.py`) and when it is sent (`compliance.deliverable` in `delivery/dispatch.py`). Keep both.
4. **Personal data:**
   - Deceased and unclaimed-estate events stay minimised: no names and no full addresses.
   - They go only to vetted EstateSignal orgs, after the cooling-off period.
   - Personal data is never sent to the AI model.
   - Personal insolvency (bankruptcy, IVA, DRO) is never routed.
5. **Staff is a database role only**, set with `scripts/make_staff.py`. Never derive it from an email address.
6. **Plans live in `app/plans.py`.** Annual price = 10 × monthly. Trials always get Team features.
7. **SQL must work on SQLite and Postgres.** Use `?` placeholders. No SQLite-only functions in app code. Wrap case-insensitive matches in `UPPER()`.
8. **Every bug fix or new feature gets a test** in `tests/`.
9. **Company Insight is information, not advice.** Keep reasons and sources on every score, keep personal-data events out of model inputs, and keep the two-person rule for more exposure to High/Critical companies.
10. **Keep `LL_DRY_RUN_DELIVERY=true`** until a real test send to the owner's own email or Slack has been checked.

## Outreach module (app/outreach/)
- Console at `/outreach` (staff only). Sending is in `engine.py`, reply and bounce reading in `inbox.py`, lead rules in `leads.py`.
- Never remove the safeguards: the PECR blocks (personal mailboxes and sole traders), suppression checks at send time, List-Unsubscribe headers and footer, caps and warm-up, sending windows.
- Mailbox passwords live only in `.env` (`OUTREACH_PW_*`), never in the database.
- First live test: one mailbox, one campaign, 2–3 leads that are the owner's own addresses, `LL_DRY_RUN_DELIVERY=false`.

## Things still to verify against live services
- **The Gazette feed:** check the URL and JSON shape in `app/sources/gazette.py` (`FEED_URL`, `_entries`, `NOTICE_CODES`) against a real response. Then add a test fixture from a real notice, with personal data removed.
- **Companies House stream:** check the message shapes (`handle_stream_line`) against real stream output.
- **Home Office sponsor register:** check the CSV link discovery and column names.
- **Stripe:** test the products and prices, and run the webhook in test mode with `stripe listen`.
- **Field counts:** the brand websites advertise 12/35/65/78 fields per level. The backend's real counts come from `GET /v1/exports/fields.csv?brand=…`. Make the sites match the backend.

## Going live
1. Ask the owner which host to use (e.g. Railway, Render, Fly.io, a UK VPS, AWS). For UK data residency, prefer a London region.
2. Create the database. Set every variable in `.env.example`. `LL_SECRET_KEY` must be a random string of 32+ characters.
3. Deploy the `api` and `worker` services. Point a domain at the API (e.g. `api.<brand domain>`) with HTTPS.
4. Run `python -m scripts.make_staff` for the owner.
5. Host the 9 websites on their own domains. Add `web/ledgerlight-client.js` and list every site origin in `LL_ALLOWED_ORIGINS`.
6. Set up the Stripe webhook to `https://<api>/v1/billing/webhook`.
7. Turn off dry-run delivery only after a successful test send.
