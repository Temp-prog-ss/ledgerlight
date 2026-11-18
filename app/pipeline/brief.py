"""Plain-English briefs. One brief per event (shared by all customers), written
from structured facts only. Uses Claude when ANTHROPIC_API_KEY is set, else a
deterministic template. The model is told to use only the facts provided."""
import json

from app import config, db
from app.sources.http import session

SYSTEM = (
    "You write short alerts for UK business professionals about public company events. "
    "Use ONLY the facts in the JSON you are given. Never guess names, amounts, dates or outcomes. "
    "Plain English, no jargon, no hype. Reply with JSON: {\"brief\": \"2-3 sentences\", \"next_action\": \"under 12 words\"}."
)

ACTIONS = {
    "winding_up_petition": "Check your exposure and act before the hearing",
    "administration": "Lodge your claim with the administrator; stop credit",
    "cvl": "Lodge your claim with the liquidator; stop credit",
    "compulsory_liquidation": "Lodge your claim with the Official Receiver",
    "liquidator_appointed": "Lodge your claim with the liquidator",
    "cva": "Review the proposal and vote before the deadline",
    "restructuring_plan": "Review the plan and consider objecting",
    "strike_off_proposed": "Object now if the company owes you money",
    "accounts_overdue": "Review credit terms; watch for further signs",
    "cs_overdue": "Watch for a strike-off notice",
    "new_charge": "Check who the lender is and watch closely",
    "ccj": "Chase payment or tighten terms",
    "director_resigned": "Watch for further changes",
    "sponsor_removed": "Contact the employer; workers may need a new sponsor",
    "sponsor_added": "Offer onboarding and compliance support",
    "sponsor_rating_change": "Offer an action plan to restore the rating",
    "deceased_estate": "Approach the executor firm only, after cooling-off",
    "unclaimed_estate": "Research next of kin through proper channels",
    "land_owned_by_dissolved": "Check title and refer to Bona Vacantia if relevant",
    "incorporated": "Send a welcome letter",
    "id_verification_due": "Offer an identity verification service",
}


def facts(event, company, net, score_reasons):
    d = db.loads(event.get("data"), {}) if isinstance(event.get("data"), str) else (event.get("data") or {})
    c = company or {}
    f = {"event": event["event_type"].replace("_", " "), "date": (event.get("published_at") or "")[:10],
         "company": c.get("name") or event.get("title"), "sector": c.get("sic_description") or c.get("sic_codes"),
         "town": c.get("town"), "employees": c.get("employees"), "turnover_gbp": c.get("turnover"),
         "net_assets_gbp": c.get("net_assets"), "lender_charges_outstanding": c.get("charges_outstanding"),
         "hearing_date": d.get("hearing_date"), "petitioner": d.get("petitioner"), "claim_deadline": d.get("claim_deadline"),
         "practitioner_firm": d.get("ip_firm"), "sponsor": db.loads(c.get("sponsor")) if c.get("sponsor") else None,
         "directors_prior_failed_companies": net.get("prior_insolvencies"), "phoenix_pattern": net.get("phoenix_flag"),
         "score_reasons": score_reasons}
    if event.get("personal_data"):  # never send personal details to the model
        f = {"event": f["event"], "date": f["date"], "claim_deadline": d.get("claim_deadline"),
             "executor_firm": d.get("executor_firm"), "area": d.get("postcode_district") or d.get("place_of_death")}
    return {k: v for k, v in f.items() if v not in (None, "", [], {})}


def template(event, f):
    what = f.get("event", "event").capitalize()
    who = f.get("company") or "A company"
    bits = [f"{what} recorded for {who}" + (f" in {f['town']}" if f.get("town") else "") + "."]
    if f.get("employees") or f.get("turnover_gbp"):
        bits.append("Latest accounts show" + (f" {f['employees']} staff" if f.get("employees") else "")
                    + (" and" if f.get("employees") and f.get("turnover_gbp") else "")
                    + (f" £{f['turnover_gbp']:,} turnover" if f.get("turnover_gbp") else "") + ".")
    if f.get("hearing_date"):
        bits.append(f"The petition hearing is on {f['hearing_date']}.")
    if f.get("claim_deadline"):
        bits.append(f"Claims must be lodged by {f['claim_deadline']}.")
    if f.get("phoenix_pattern"):
        bits.append("Its directors also run a newer company in the same trade.")
    if event.get("personal_data"):
        bits = [f"{what} published" + (f" for the {f['area']} area" if f.get("area") else "") + "."]
        if f.get("executor_firm"):
            bits.append(f"The executor is {f['executor_firm']}.")
        if f.get("claim_deadline"):
            bits.append(f"Claims deadline: {f['claim_deadline']}.")
    return " ".join(bits), ACTIONS.get(event["event_type"], "Review and decide whether to act")


def write(event, f):
    """Return (brief, next_action, model)."""
    key = config.settings.anthropic_api_key
    if not key:
        b, a = template(event, f)
        return b, a, "template"
    try:
        r = session().post("https://api.anthropic.com/v1/messages", timeout=40, headers={
            "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": config.settings.anthropic_model, "max_tokens": 300, "system": SYSTEM,
                  "messages": [{"role": "user", "content": json.dumps(f, ensure_ascii=False, default=str)}]})
        r.raise_for_status()
        text = "".join(p.get("text", "") for p in r.json().get("content", []) if p.get("type") == "text")
        out = json.loads(text[text.find("{"): text.rfind("}") + 1])
        return out["brief"].strip(), out.get("next_action", "").strip() or ACTIONS.get(event["event_type"], ""), config.settings.anthropic_model
    except Exception as e:  # never block the pipeline on the model
        db.audit("worker", "brief_fallback", {"event": event["id"], "error": str(e)[:200]})
        b, a = template(event, f)
        return b, a, "template"
