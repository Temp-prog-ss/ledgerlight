"""Field catalogue: every extractable field, its level and which brands show it.

`project(ctx, level, brand)` turns an event + company (+ watchlist item) into
the dict of fields the customer's plan allows. The same projection feeds the
dashboard, alerts and every CSV export, so what a customer sees always matches
what they can download.
"""
from app.db import loads

E = lambda k: (lambda c: (c["event"].get("data") or {}).get(k))  # noqa: E731  event data getter
CO = lambda k: (lambda c: (c.get("company") or {}).get(k))       # noqa: E731  company getter
W = lambda k: (lambda c: (c.get("watch") or {}).get(k))          # noqa: E731  watchlist item getter


def _directors(c):
    d = loads((c.get("company") or {}).get("directors"), [])
    return "; ".join(x.get("name", "") for x in d if not x.get("resigned_on"))


def _sponsor(k):
    return lambda c: (loads((c.get("company") or {}).get("sponsor"), {}) or {}).get(k)


def _net_change(c):
    co = c.get("company") or {}
    a, b = co.get("net_assets"), co.get("net_assets_prev")
    if a is None or not b:
        return None
    return f"{round((a - b) / abs(b) * 100)}%"


# key, label, description, level, brands (None = all), getter
FIELDS = [
    ("event_id", "Event ID", "Stable Ledgerlight identifier", 1, None, lambda c: c["event"]["id"]),
    ("event_type", "Event type", "What happened", 1, None, lambda c: c["event"]["event_type"].replace("_", " ")),
    ("event_date", "Event date", "Published or effective date", 1, None, lambda c: (c["event"].get("effective_date") or c["event"].get("published_at") or "")[:10]),
    ("notice_url", "Notice link", "Original notice or register entry", 1, None, E("url")),
    ("company_name", "Company name", "Registered name", 1, None, lambda c: (c.get("company") or {}).get("name") or c["event"].get("title")),
    ("company_number", "Company number", "Companies House number", 1, None, lambda c: c["event"].get("company_number")),
    ("company_status", "Company status", "Status on the register", 1, None, CO("status")),
    ("ro_town", "Town", "Registered office town", 1, None, CO("town")),
    ("ro_postcode", "Postcode", "Registered office postcode", 1, None, CO("postcode")),
    ("sic_description", "Sector", "SIC description", 2, None, CO("sic_description")),
    ("sic_codes", "SIC codes", "Standard Industrial Classification", 2, None, CO("sic_codes")),
    ("region", "Region", "ONS region", 2, None, CO("region")),
    ("ip_name", "Practitioner", "Appointed insolvency practitioner", 2, None, E("ip_name")),
    ("ip_firm", "Practitioner firm", "IP firm", 2, None, E("ip_firm")),
    ("ip_phone", "IP phone", "As published in the notice", 2, None, E("ip_phone")),
    ("ip_email", "IP email", "As published in the notice", 2, None, E("ip_email")),
    ("court", "Court", "Court handling the case", 2, None, E("court")),
    ("case_number", "Case number", "Court reference", 2, None, E("case_number")),
    ("hearing_date", "Hearing date", "Petition hearing", 2, None, E("hearing_date")),
    ("petitioner", "Petitioner", "Who brought the petition", 2, None, E("petitioner")),
    ("claim_deadline", "Claim deadline", "Last date to prove debts", 2, None, E("claim_deadline")),
    ("accounts_overdue", "Accounts overdue", "Companies House filing status", 2, None, lambda c: bool((c.get("company") or {}).get("accounts_overdue"))),
    ("accounts_next_due", "Accounts due", "Next accounts due date", 2, None, CO("accounts_next_due")),
    ("cs_next_due", "Confirmation statement due", "Next due date", 2, None, CO("cs_next_due")),
    ("ccj_count_12m", "CCJs (12m)", "County court judgments in the last year", 2, None, CO("ccj_count_12m")),
    ("incorporated_on", "Incorporated", "Date of incorporation", 2, None, CO("incorporated_on")),
    ("directors", "Directors", "Current directors (business role)", 3, None, _directors),
    ("recent_resignations", "Resignations (12m)", "Directors resigned in the last year", 3, None, CO("recent_resignations")),
    ("charges_outstanding", "Charges outstanding", "Unsatisfied lender charges", 3, None, CO("charges_outstanding")),
    ("latest_lender", "Latest lender", "Most recent charge holder", 3, None, CO("latest_lender")),
    ("turnover_gbp", "Turnover (£)", "Latest filed accounts", 3, None, CO("turnover")),
    ("employees", "Employees", "Average headcount", 3, None, CO("employees")),
    ("net_assets_gbp", "Net assets (£)", "Latest balance sheet", 3, None, CO("net_assets")),
    ("net_assets_change", "Net assets change", "Year on year", 3, None, _net_change),
    ("risk_score", "Risk score", "0–100 distress score", 3, None, lambda c: c["event"].get("risk_score")),
    ("relevance", "Relevance", "0–100 fit to this product", 3, None, lambda c: c.get("relevance")),
    ("ai_brief", "AI brief", "Plain-English summary", 3, None, lambda c: c["event"].get("brief")),
    ("next_action", "Next action", "Recommended step", 3, None, lambda c: c["event"].get("next_action")),
    ("reasons", "Score reasons", "Why it scored as it did", 3, None, lambda c: "; ".join(loads(c["event"].get("reasons"), []))),
    ("connected_cos", "Connected companies", "Companies sharing directors", 4, None, lambda c: c.get("connected_count")),
    ("phoenix_flag", "Phoenix flag", "New firm by same directors in the same trade", 4, None, lambda c: c.get("phoenix_flag")),
    ("website", "Website", "Business website", 4, None, CO("website")),
    ("business_phone", "Business phone", "Switchboard, TPS/CTPS-screened", 4, None, CO("phone")),
    ("subscriber_type", "PECR subscriber type", "Corporate or individual (sole trader)", 4, None, CO("subscriber_type")),
    # Brand-specific
    ("public_contracts_gbp", "Public contracts (£)", "Value of public awards, 3 years", 3, {"sitesentinel", "ledgerlight"}, CO("public_contracts_value")),
    ("retention_at_risk_gbp", "Retention at risk (£)", "From your watchlist", 2, {"sitesentinel"}, W("amount_owed")),
    ("sponsor_route", "Sponsor route", "Skilled Worker, Health & Care…", 1, {"sponsorwatch", "ledgerlight"}, _sponsor("route")),
    ("sponsor_rating", "Sponsor rating", "A or B", 2, {"sponsorwatch", "ledgerlight"}, _sponsor("rating")),
    ("sponsor_town", "Sponsor town", "Town on the register", 2, {"sponsorwatch"}, _sponsor("town")),
    ("register_first_seen", "First seen on register", "Date first listed", 4, {"sponsorwatch"}, _sponsor("first_seen")),
    ("executor_firm", "Executor firm", "Professional executor, if named", 2, {"estatesignal"}, E("executor_firm")),
    ("postcode_district", "Postcode district", "Area only, never a full address", 1, {"estatesignal"}, E("postcode_district")),
    ("cooling_off_until", "Cooling-off until", "Earliest permitted approach", 1, {"estatesignal"}, lambda c: c["event"].get("restricted_until")),
    ("titles_owned", "Land titles owned", "HM Land Registry", 3, {"estatesignal", "tenantguard", "ledgerlight"}, CO("titles_owned")),
    ("title_numbers", "Title numbers", "HM Land Registry", 4, {"estatesignal", "tenantguard", "ledgerlight"}, lambda c: "; ".join(loads((c.get("company") or {}).get("title_numbers"), []))),
    ("director_prior_insolv", "Directors' prior insolvencies", "Across connected companies", 3, {"distressradar", "ledgerlight"}, lambda c: c.get("prior_insolvencies")),
    ("your_units", "Your units", "From your tenant schedule", 1, {"tenantguard"}, W("label")),
    ("annual_rent_gbp", "Annual rent at risk (£)", "From your tenant schedule", 2, {"tenantguard"}, W("amount_owed")),
    ("your_unpaid_invoices_gbp", "Your unpaid invoices (£)", "From your client list", 2, {"talentrelease"}, W("amount_owed")),
    ("amount_owed_gbp", "Amount owed to you (£)", "From your debtor list", 1, {"creditpulse"}, W("amount_owed")),
    ("credit_limit_gbp", "Your credit limit (£)", "From your settings", 2, {"creditpulse"}, W("credit_limit")),
    ("id_verified_pct", "Officers ID-verified", "Share of directors/PSCs verified", 2, {"filingpulse", "ledgerlight"}, CO("id_verified_pct")),
    ("accounts_type", "Accounts type", "micro, small, full…", 3, {"filingpulse", "ledgerlight"}, CO("accounts_type")),
]


def catalogue(brand="ledgerlight"):
    return [f for f in FIELDS if f[4] is None or brand in f[4]]


def columns(level, brand="ledgerlight"):
    return [f for f in catalogue(brand) if f[3] <= level]


def project(ctx, level, brand="ledgerlight"):
    out = {}
    for key, _label, _desc, _lvl, _brands, getter in columns(level, brand):
        try:
            out[key] = getter(ctx)
        except Exception:  # a missing source field never breaks a row
            out[key] = None
    return out


def dictionary_rows(brand="ledgerlight"):
    from app.plans import LEVELS
    return [{"field_key": k, "label": lab, "description": d, "minimum_level": LEVELS[lvl], "level": lvl}
            for k, lab, d, lvl, _b, _g in catalogue(brand)]
