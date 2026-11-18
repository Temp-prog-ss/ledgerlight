"""Merge fields and the default 3-step sequence for each brand.

Merge syntax: {{first_name}}, {{company}}, {{position}}, {{website}}, {{sender_name}}, {{brand}},
{{brand_url}}, with an optional fallback: {{first_name|there}}.
Plain text on purpose: it lands in inboxes far more reliably than designed HTML for cold email.
"""
import os
import re

from app import brands

MERGE_RX = re.compile(r"\{\{\s*([a-z_]+)\s*(?:\|([^}]*))?\}\}")


def render(text, values):
    def sub(m):
        v = values.get(m.group(1))
        return str(v).strip() if v not in (None, "") else (m.group(2) or "").strip()
    return MERGE_RX.sub(sub, text or "")


def missing_fields(text, values):
    return sorted({m.group(1) for m in MERGE_RX.finditer(text or "") if m.group(2) is None and not values.get(m.group(1))})


def footer(unsubscribe_url):
    company = os.environ.get("OUTREACH_COMPANY_NAME", "Ledgerlight Ltd")
    address = os.environ.get("OUTREACH_POSTAL_ADDRESS", "[set OUTREACH_POSTAL_ADDRESS]")
    return (f"\n\n--\n{company}, {address}\n"
            f"We contacted you because your business may find this useful. "
            f"Not interested? Unsubscribe here: {unsubscribe_url} (or just reply 'unsubscribe').")


PITCH = {
    "ledgerlight": dict(pain="finding out late when a customer, supplier or partner gets into trouble",
                        what="alerts on insolvencies, petitions and late filings for the companies you deal with",
                        proof="We cover every UK company on the Companies House register and every Gazette insolvency notice."),
    "sitesentinel": dict(pain="hearing that a main contractor has gone under from site gossip, after the retention has gone",
                         what="an alert the day a contractor or client you work with gets a petition, goes into administration or files late",
                         proof="Construction has had more insolvencies than any other UK sector over the past year."),
    "sponsorwatch": dict(pain="licensed sponsors failing or losing their licence, leaving workers with weeks to find a new sponsor",
                         what="a daily feed matching the Home Office sponsor register against insolvency notices and register removals",
                         proof="Sponsor licence revocations more than doubled in the year to June 2026."),
    "estatesignal": dict(pain="slow, scattered sources for probate and estate work",
                         what="Gazette deceased-estate notices matched to executor firms and property, for vetted firms only",
                         proof="Around 39,000 deceased-estate notices are published in The Gazette each year."),
    "distressradar": dict(pain="reaching distressed companies after another practitioner already has",
                          what="a daily distress score for every company: HMRC petitions, overdue filings, new lender charges and repeat directors",
                          proof="Each alert explains why the company scored as it did, so you can prioritise in minutes."),
    "tenantguard": dict(pain="finding out a tenant is insolvent after the rent has stopped",
                        what="monitoring of every tenant on your schedule, plus land held by dissolved companies",
                        proof="Upload your tenant list once and we watch every company daily."),
    "talentrelease": dict(pain="competitors reaching good candidates first when an employer fails",
                          what="same-day alerts when employers with 20+ staff enter administration, filtered by sector and distance",
                          proof="Each alert includes headcount and likely roles, from the company's own accounts."),
    "creditpulse": dict(pain="customers going bust while they still owe you money",
                        what="daily monitoring of every customer on your ledger, ranked by how much they owe you",
                        proof="It's £0.20 per customer per month, with no setup and no integration."),
    "filingpulse": dict(pain="spending time finding new clients",
                        what="a daily list of new companies near you, and companies behind on accounts or ID checks, with a ready-to-send letter",
                        proof="Over 800,000 companies are incorporated in the UK each year."),
}


def default_sequence(brand_id):
    b = brands.get(brand_id) or brands.get("ledgerlight")
    p = PITCH.get(b.id, PITCH["ledgerlight"])
    return [
        {"step_no": 1, "delay_days": 0, "subject": f"Quick question about {{{{company|your business}}}}",
         "body": (f"Hi {{{{first_name|there}}}},\n\n"
                  f"Do you ever have problems with {p['pain']}?\n\n"
                  f"We built {b.name} to fix that: {p['what']}. {p['proof']}\n\n"
                  f"Would a free 14-day trial be useful for {{{{company|your team}}}}? Just reply and I'll set it up.\n\n"
                  f"{{{{sender_name}}}}\n{b.name}")},
        {"step_no": 2, "delay_days": 3, "subject": None,
         "body": (f"Hi {{{{first_name|there}}}},\n\n"
                  f"Following up in case my last note got buried. If it helps, I can send a sample of last week's {b.name} alerts "
                  f"for your area so you can judge whether they're useful.\n\n"
                  f"Worth a look?\n\n{{{{sender_name}}}}")},
        {"step_no": 3, "delay_days": 7, "subject": None,
         "body": (f"Hi {{{{first_name|there}}}},\n\n"
                  f"I won't keep filling your inbox, so this is my last note. If {p['pain']} isn't an issue for "
                  f"{{{{company|you}}}} right now, no problem at all.\n\n"
                  f"If it ever is, the trial is here: {{{{brand_url}}}}\n\n{{{{sender_name}}}}")},
    ]
