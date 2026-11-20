-- Portable DDL (SQLite + Postgres). Timestamps are ISO-8601 text, JSON is text.
CREATE TABLE IF NOT EXISTS companies (
  number TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  status TEXT,
  company_type TEXT,
  incorporated_on TEXT,
  dissolved_on TEXT,
  sic_codes TEXT,
  sic_description TEXT,
  address_line TEXT,
  town TEXT,
  postcode TEXT,
  region TEXT,
  accounts_next_due TEXT,
  accounts_overdue INTEGER DEFAULT 0,
  accounts_type TEXT,
  last_accounts_to TEXT,
  cs_next_due TEXT,
  cs_overdue INTEGER DEFAULT 0,
  turnover INTEGER,
  employees INTEGER,
  net_assets INTEGER,
  net_assets_prev INTEGER,
  charges_outstanding INTEGER DEFAULT 0,
  latest_lender TEXT,
  latest_charge_date TEXT,
  directors TEXT,
  pscs TEXT,
  recent_resignations INTEGER DEFAULT 0,
  id_verified_pct INTEGER,
  website TEXT,
  phone TEXT,
  subscriber_type TEXT DEFAULT 'corporate',
  ccj_count_12m INTEGER DEFAULT 0,
  sponsor TEXT,
  titles_owned INTEGER DEFAULT 0,
  title_numbers TEXT,
  public_contracts_value INTEGER,
  raw TEXT,
  enriched_at TEXT,
  updated_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_companies_name ON companies(name);
CREATE INDEX IF NOT EXISTS ix_companies_postcode ON companies(postcode);

CREATE TABLE IF NOT EXISTS officers (
  id TEXT PRIMARY KEY,
  company_number TEXT NOT NULL,
  officer_key TEXT,
  name TEXT,
  role TEXT,
  appointed_on TEXT,
  resigned_on TEXT
);
CREATE INDEX IF NOT EXISTS ix_officers_company ON officers(company_number);
CREATE INDEX IF NOT EXISTS ix_officers_key ON officers(officer_key);

CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  source_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  category TEXT,
  company_number TEXT,
  title TEXT,
  published_at TEXT,
  effective_date TEXT,
  data TEXT,
  personal_data INTEGER DEFAULT 0,
  risk_score INTEGER,
  reasons TEXT,
  brief TEXT,
  brief_model TEXT,
  next_action TEXT,
  status TEXT DEFAULT 'new',
  restricted_until TEXT,
  created_at TEXT,
  UNIQUE (source, source_id)
);
CREATE INDEX IF NOT EXISTS ix_events_company ON events(company_number);
CREATE INDEX IF NOT EXISTS ix_events_status ON events(status);
CREATE INDEX IF NOT EXISTS ix_events_published ON events(published_at);

CREATE TABLE IF NOT EXISTS event_brands (
  event_id TEXT NOT NULL,
  brand TEXT NOT NULL,
  relevance INTEGER,
  PRIMARY KEY (event_id, brand)
);
CREATE INDEX IF NOT EXISTS ix_event_brands_brand ON event_brands(brand);

CREATE TABLE IF NOT EXISTS orgs (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  brand TEXT NOT NULL,
  plan TEXT NOT NULL DEFAULT 'solo',
  billing_cycle TEXT DEFAULT 'monthly',
  status TEXT NOT NULL DEFAULT 'trialing',
  trial_ends_at TEXT,
  stripe_customer_id TEXT,
  stripe_subscription_id TEXT,
  vetted INTEGER DEFAULT 0,
  regions TEXT,
  timezone TEXT DEFAULT 'Europe/London',
  digest_hour INTEGER DEFAULT 7,
  last_digest_at TEXT,
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL REFERENCES orgs(id),
  email TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  name TEXT,
  role TEXT NOT NULL DEFAULT 'owner',
  created_at TEXT,
  last_login TEXT
);

CREATE TABLE IF NOT EXISTS watchlists (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL REFERENCES orgs(id),
  name TEXT NOT NULL,
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS watchlist_items (
  id TEXT PRIMARY KEY,
  watchlist_id TEXT NOT NULL REFERENCES watchlists(id) ON DELETE CASCADE,
  company_number TEXT NOT NULL,
  label TEXT,
  amount_owed INTEGER,
  credit_limit INTEGER,
  extra TEXT,
  created_at TEXT,
  UNIQUE (watchlist_id, company_number)
);
CREATE INDEX IF NOT EXISTS ix_watch_company ON watchlist_items(company_number);

CREATE TABLE IF NOT EXISTS channels (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL REFERENCES orgs(id),
  kind TEXT NOT NULL,
  target TEXT NOT NULL,
  config TEXT,
  active INTEGER DEFAULT 1,
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS alert_rules (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL REFERENCES orgs(id),
  name TEXT NOT NULL,
  event_types TEXT,
  regions TEXT,
  sic_prefixes TEXT,
  keywords TEXT,
  min_score INTEGER DEFAULT 0,
  watchlist_only INTEGER DEFAULT 0,
  instant INTEGER DEFAULT 1,
  digest INTEGER DEFAULT 1,
  channel_ids TEXT,
  active INTEGER DEFAULT 1,
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS deliveries (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL,
  event_id TEXT NOT NULL,
  channel_id TEXT NOT NULL,
  mode TEXT NOT NULL DEFAULT 'instant',
  status TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER DEFAULT 0,
  last_error TEXT,
  not_before TEXT,
  sent_at TEXT,
  created_at TEXT,
  UNIQUE (org_id, event_id, channel_id, mode)
);
CREATE INDEX IF NOT EXISTS ix_deliveries_status ON deliveries(status);

CREATE TABLE IF NOT EXISTS usage (
  org_id TEXT NOT NULL,
  month TEXT NOT NULL,
  briefs INTEGER DEFAULT 0,
  exports INTEGER DEFAULT 0,
  export_rows INTEGER DEFAULT 0,
  insight_lookups INTEGER DEFAULT 0,
  PRIMARY KEY (org_id, month)
);

CREATE TABLE IF NOT EXISTS suppressions (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  value TEXT NOT NULL,
  reason TEXT,
  created_at TEXT,
  UNIQUE (kind, value)
);

CREATE TABLE IF NOT EXISTS risk_factors (
  id TEXT PRIMARY KEY,
  brand TEXT NOT NULL,
  title TEXT NOT NULL,
  why TEXT,
  watch TEXT,
  fact TEXT,
  source_url TEXT,
  plan_note TEXT,
  source_hash TEXT,
  last_checked TEXT,
  needs_review INTEGER DEFAULT 0,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS export_jobs (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL,
  params TEXT,
  status TEXT DEFAULT 'queued',
  file_path TEXT,
  content TEXT,
  row_count INTEGER,
  error TEXT,
  created_at TEXT,
  finished_at TEXT
);

CREATE TABLE IF NOT EXISTS sponsor_register (
  key TEXT PRIMARY KEY,
  org_name TEXT,
  town TEXT,
  county TEXT,
  rating TEXT,
  route TEXT,
  company_number TEXT,
  first_seen TEXT,
  last_seen TEXT,
  removed_at TEXT
);

CREATE TABLE IF NOT EXISTS unclaimed_estates (
  id TEXT PRIMARY KEY,
  data TEXT,
  first_seen TEXT,
  last_seen TEXT,
  removed_at TEXT
);

CREATE TABLE IF NOT EXISTS ingest_state (
  key TEXT PRIMARY KEY,
  value TEXT,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
  id TEXT PRIMARY KEY,
  actor TEXT,
  action TEXT,
  detail TEXT,
  at TEXT
);

-- ===================== Outreach (selling our own products) =====================
CREATE TABLE IF NOT EXISTS outreach_mailboxes (
  id TEXT PRIMARY KEY,
  email TEXT NOT NULL UNIQUE,
  from_name TEXT,
  smtp_host TEXT NOT NULL,
  smtp_port INTEGER DEFAULT 587,
  smtp_user TEXT,
  password_env TEXT,
  imap_host TEXT,
  imap_port INTEGER DEFAULT 993,
  daily_cap INTEGER DEFAULT 40,
  warmup_start INTEGER DEFAULT 10,
  warmup_step INTEGER DEFAULT 3,
  sent_date TEXT,
  sent_today INTEGER DEFAULT 0,
  last_sent_at TEXT,
  last_imap_uid INTEGER DEFAULT 0,
  active INTEGER DEFAULT 1,
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS outreach_leads (
  id TEXT PRIMARY KEY,
  email TEXT NOT NULL UNIQUE,
  first_name TEXT,
  last_name TEXT,
  position TEXT,
  company TEXT,
  website TEXT,
  company_number TEXT,
  brand TEXT,
  source TEXT,
  date_found TEXT,
  stage TEXT DEFAULT 'new',
  subscriber_type TEXT,
  block_reason TEXT,
  notes TEXT,
  last_contacted_at TEXT,
  replied_at TEXT,
  created_at TEXT,
  updated_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_outreach_leads_stage ON outreach_leads(stage);

CREATE TABLE IF NOT EXISTS outreach_campaigns (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  brand TEXT NOT NULL,
  mailbox_ids TEXT,
  status TEXT DEFAULT 'draft',
  window_start INTEGER DEFAULT 9,
  window_end INTEGER DEFAULT 17,
  weekdays TEXT DEFAULT '[0,1,2,3,4]',
  timezone TEXT DEFAULT 'Europe/London',
  min_gap_seconds INTEGER DEFAULT 90,
  max_gap_seconds INTEGER DEFAULT 240,
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS outreach_steps (
  id TEXT PRIMARY KEY,
  campaign_id TEXT NOT NULL,
  step_no INTEGER NOT NULL,
  delay_days INTEGER DEFAULT 0,
  subject TEXT,
  body TEXT NOT NULL,
  UNIQUE (campaign_id, step_no)
);

CREATE TABLE IF NOT EXISTS outreach_enrollments (
  id TEXT PRIMARY KEY,
  campaign_id TEXT NOT NULL,
  lead_id TEXT NOT NULL,
  status TEXT DEFAULT 'active',
  next_step INTEGER DEFAULT 1,
  next_send_at TEXT,
  mailbox_id TEXT,
  first_message_id TEXT,
  first_subject TEXT,
  created_at TEXT,
  UNIQUE (campaign_id, lead_id)
);
CREATE INDEX IF NOT EXISTS ix_enrol_due ON outreach_enrollments(status, next_send_at);

CREATE TABLE IF NOT EXISTS outreach_messages (
  id TEXT PRIMARY KEY,
  enrollment_id TEXT,
  lead_id TEXT,
  mailbox_id TEXT,
  step_no INTEGER,
  message_id TEXT,
  subject TEXT,
  status TEXT,
  error TEXT,
  sent_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_outreach_msgid ON outreach_messages(message_id);

CREATE TABLE IF NOT EXISTS outreach_activity (
  id TEXT PRIMARY KEY,
  lead_id TEXT,
  kind TEXT,
  detail TEXT,
  at TEXT
);

-- Company Insight: monthly risk snapshots (training data), customer decisions, news, sector base rates.
CREATE TABLE IF NOT EXISTS insight_snapshots (
  company_number TEXT NOT NULL,
  month TEXT NOT NULL,
  sic_section TEXT,
  features TEXT,
  probability REAL,
  band TEXT,
  model_version TEXT,
  label_failed INTEGER,
  labelled_at TEXT,
  created_at TEXT,
  PRIMARY KEY (company_number, month)
);
CREATE INDEX IF NOT EXISTS ix_insight_snap_month ON insight_snapshots(month, sic_section);

CREATE TABLE IF NOT EXISTS insight_decisions (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  company_number TEXT NOT NULL,
  band TEXT,
  probability REAL,
  action TEXT NOT NULL,
  note TEXT,
  second_reviewer TEXT,
  outcome TEXT,
  outcome_at TEXT,
  created_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_insight_dec_org ON insight_decisions(org_id, company_number);

CREATE TABLE IF NOT EXISTS news_mentions (
  id TEXT PRIMARY KEY,
  company_number TEXT NOT NULL,
  url TEXT NOT NULL,
  title TEXT,
  domain TEXT,
  published_at TEXT,
  flagged INTEGER DEFAULT 0,
  matched_terms TEXT,
  created_at TEXT,
  UNIQUE (company_number, url)
);
CREATE INDEX IF NOT EXISTS ix_news_company ON news_mentions(company_number, published_at);

CREATE TABLE IF NOT EXISTS base_rates (
  sic_section TEXT PRIMARY KEY,
  annual_rate REAL NOT NULL,
  source TEXT,
  updated_at TEXT
);
