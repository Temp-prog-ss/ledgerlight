"""Build the Playbooks / Platform / Terminal block that is added to each of the nine websites.

One source of truth: data/playbooks.json (also served in the portal). Run:
    python -m scripts.build_site_blocks            # writes build/site_blocks/<brand>.{css,html,js}
The website pages are static; paste or re-apply the generated blocks when playbooks change.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = "https://ledgerlight-api.onrender.com"   # the live API/portal; change here if the Render URL differs
SITES = {"ledgerlight": "https://claude.ai/artifact/GQWgFUXpdo5uVGt89ooMGb",
         "sitesentinel": "https://claude.ai/artifact/NdL4NRozMoK4kH95C86VRD",
         "sponsorwatch": "https://claude.ai/artifact/Y5sbbDbpT5hmc7F6m6YAhW",
         "estatesignal": "https://claude.ai/artifact/Xnjpe31KgXc5HZzQFq1DWX",
         "distressradar": "https://claude.ai/artifact/T6PH8DceswoZUThyBjPQct",
         "tenantguard": "https://claude.ai/artifact/1PRcChcX6ncbtUmbsRkxva",
         "talentrelease": "https://claude.ai/artifact/M2MKVLCZsKYQYTmnuwo1Lf",
         "creditpulse": "https://claude.ai/artifact/SikDycyXemmqBg41KNcZYx",
         "filingpulse": "https://claude.ai/artifact/NJ5XepbM9oZv1yRgwX5sm1"}

# Sample terminal wire per brand: invented headlines, clearly labelled on the page.
WIRE = {
 "sitesentinel": [
  ["ADMIN", "Main contractor Harbourline Build calls in administrators", "Ledgerlight · Gazette", "Clients on 9 live sites now need a contractor, and every subcontractor holding retention is an unsecured creditor.", ["Lodge your claim before the deadline", "Offer completion proposals to the stalled sites' clients"], "ss-takeover-work"],
  ["PETITION", "HMRC petitions to wind up Oakmere Groundworks", "Ledgerlight · Gazette", "A petition means payments the company makes from now can be clawed back. Your exposure is what is owed today.", ["Stop supplying on credit", "Take advice before accepting new payments"], "ss-retention"],
  ["NEWS", "Plant hire group shuts three depots as rates fall", "Trade press (sample)", "Depot closures often lead to equipment sales, and local contractors lose a supplier.", ["Register interest in the plant with the agent", "Offer accounts to its customers after a credit check"], "ss-assets"]],
 "sponsorwatch": [
  ["SPONSOR", "Care operator with 30 sponsored workers enters liquidation", "Ledgerlight · Gazette + Home Office", "Its sponsored workers usually have about 60 days to find a new sponsor, and nearby care employers have vacancies.", ["List licensed care sponsors within 20 miles", "Offer them a fixed-fee sponsorship package"], "sw-switch"],
  ["REGISTER", "12 new sponsor licences granted in the West Midlands this week", "Ledgerlight · Home Office", "New sponsors often miss reporting duties in their first year.", ["Offer a mock compliance audit", "Turn audits into retainers"], "sw-audits"],
  ["NEWS", "Care funding squeeze hits small providers (sample)", "Sector press (sample)", "Your sponsoring clients in care may slow hiring or fail.", ["Check clients' risk bands", "Pause new placements with Elevated clients"], "sw-protect"]],
 "estatesignal": [
  ["ESTATE", "6 estate notices in Bristol handled by professional firms (after cooling-off)", "Ledgerlight · Gazette (minimised)", "Professional firms handling estates need valuations, sales and clearance.", ["Group notices by firm", "Offer the firm a business services package"], "es-firms"],
  ["UNCLAIMED", "New unclaimed estates added for your research areas", "Ledgerlight · Bona vacantia list", "New listings face the least competition from other genealogists.", ["Prioritise by date added", "Start the family-tree research"], "es-unclaimed"],
  ["LAND", "Dissolved company still holds a freehold in Leeds", "Ledgerlight · Land Registry + CH", "The property may pass to the Crown, or the company could be restored and sold.", ["Value the title", "Contact the bona vacantia office or the former directors"], "es-dissolved-land"]],
 "distressradar": [
  ["RISK", "14 companies in your region moved to High risk this week", "Ledgerlight · Company Insight", "Directors who get advice early have more options than liquidation.", ["Review the reasons for each", "Offer an honest initial review"], "dr-early"],
  ["ADMIN", "Regional bakery chain enters administration, sale process opens", "Ledgerlight · Gazette", "Administrators sell quickly, so early buyers get first look.", ["Register interest with the administrator today", "Read the risk timeline before bidding"], "dr-assets"],
  ["CHARGE", "Manufacturer takes a new charge from an alternative lender", "Ledgerlight · Companies House", "A cash squeeze with assets still in the business can be a lending opportunity.", ["Check net assets and the reasons", "Price a secured facility to the band"], "dr-lend"]],
 "tenantguard": [
  ["CCJ", "Tenant at Unit 4 gets two county court judgments", "Ledgerlight · Registry Trust", "Judgments often come months before rent arrears build up.", ["Agree a payment plan early", "Ask for a deposit at renewal"], "tg-rent"],
  ["ADMIN", "Fashion chain with 40 stores enters administration", "Ledgerlight · Gazette", "Vacant units are coming, and competitors may want them.", ["List competitors and growing firms", "Send them your available units"], "tg-relet"],
  ["LAND", "Property company in administration holds 11 titles", "Ledgerlight · Land Registry + Gazette", "Administrators need to sell, often quickly.", ["Value the titles", "Make an offer to the administrator"], "tg-buy"]],
 "talentrelease": [
  ["ADMIN", "Vantor Engineering (120 staff, Sheffield) enters administration", "Ledgerlight · Gazette", "Machinists, design engineers and project managers are looking for work this week.", ["Post your matching roles within 48 hours", "Offer the administrator a jobs day"], "tr-place"],
  ["ADMIN", "Distribution firm closes two sites, 300 roles affected", "Ledgerlight · Gazette", "Administrators and buyers often need outplacement support.", ["Send the practitioner a fixed-fee offer", "Follow up with the buyer"], "tr-outplace"],
  ["DEAL", "Software firm raises new funding and wins a public contract", "News (sample)", "Fresh funding and a contract win usually mean hiring.", ["Check its risk band", "Send the hiring manager a shortlist"], "tr-growth"]],
 "creditpulse": [
  ["RISK", "3 customers on your ledger moved to Elevated, £42k owed", "Ledgerlight · Company Insight", "Your policy says lower limits and shorter terms for Elevated customers.", ["Apply the Elevated terms today", "Record each change in the decision log"], "cp-limits"],
  ["PETITION", "Customer owing £18,400 faces a winding-up petition", "Ledgerlight · Gazette", "Payments after a petition can be clawed back, so act before it's presented if you can.", ["Stop further supply", "Take advice before accepting payment"], "cp-collect"],
  ["TREND", "Low-risk customers in food wholesale improving for 3 months", "Ledgerlight · Company Insight", "Competitors may be tightening credit in this sector while these customers stay strong.", ["Offer higher limits for more volume", "Review monthly"], "cp-grow"]],
 "filingpulse": [
  ["NEW", "214 companies incorporated within 10 miles of Leeds this week", "Ledgerlight · Companies House", "New companies choose an accountant in their first weeks.", ["Export them to your CRM", "Send a welcome letter with a first-year package"], "fp-new"],
  ["OVERDUE", "86 local companies have accounts overdue, 12 near strike-off", "Ledgerlight · Companies House", "Penalties rise the longer they wait, and many need a new accountant.", ["Prioritise those near strike-off", "Offer a fixed-fee catch-up"], "fp-overdue"],
  ["GROWTH", "31 local companies are approaching the VAT threshold", "Ledgerlight · filed accounts", "They will need VAT registration and better systems soon.", ["Offer a growth review", "Track which signals convert"], "fp-growth"]],
}

PLATFORM = [
 ["⚡", "Live feed and instant alerts", "Scored events by email, Slack, Teams or SMS within minutes, plus a 7am digest."],
 ["🔎", "Company Insight", "A 12-month risk score for any UK company, with the reasons, the trend and what to do next."],
 ["👁️", "Watchlist", "The companies you supply, rent to or are owed money by, checked every day."],
 ["📈", "Market terminal", "A live news and signals wire for your sector, with an AI analyst that explains what each item means for you."],
 ["💷", "Profit playbooks", "Step-by-step ways to turn alerts into money, built into the portal and the terminal."],
 ["⬇️", "Data extraction and CSV", "Choose your fields at four levels and download any view as a spreadsheet."],
 ["🔄", "CRM sync and file drops", "We push leads into HubSpot, Salesforce or Pipedrive, or send scheduled files. Pro and above."],
 ["✅", "Decision log", "Record every credit or risk decision and its outcome, with a second reviewer where it matters."],
]

CSS = """<style id="llx-css">
.llx-sec{padding:72px 0 20px}.llx-sec h2{margin:10px 0 12px}
.llx-chips{display:flex;flex-wrap:wrap;gap:8px;margin:20px 0 6px}
.llx-chip{border:1px solid var(--line);background:var(--card);border-radius:99px;padding:7px 13px;font:500 13.5px var(--sans);cursor:pointer;color:var(--ink2)}
.llx-chip.on{background:var(--dark,var(--ink));color:#fff;border-color:transparent}
.llx-pb{display:grid;grid-template-columns:300px 1fr;gap:20px;margin-top:20px;align-items:start}
.llx-tabs{display:flex;flex-direction:column;gap:8px}
.llx-tab{text-align:left;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;font:600 15px var(--sans);cursor:pointer;color:var(--ink);display:flex;gap:10px}
.llx-tab span{font:500 12px var(--mono);color:var(--acc,var(--green2));padding-top:2px}
.llx-tab.on{border-color:var(--acc,var(--green));box-shadow:inset 3px 0 0 var(--acc,var(--green))}
.llx-body{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:28px}
.llx-body h3{font-size:26px;margin:0 0 10px}.llx-body h4{font:600 15px var(--sans);margin:18px 0 8px}
.llx-profit{font-size:17px;color:var(--ink)}.llx-money{font-size:14px;color:var(--mute);margin-top:6px}
.llx-sig{display:flex;flex-wrap:wrap;gap:6px;margin-top:14px}.llx-sig span,.llx-f{font:500 12px var(--sans);padding:5px 9px;border-radius:99px;background:var(--tint,var(--paper2));color:var(--ink2);display:inline-block;margin:0 6px 6px 0}
.llx-steps{list-style:none;counter-reset:s;display:grid;gap:0}
.llx-steps li{counter-increment:s;position:relative;padding:11px 0 11px 44px;border-bottom:1px solid var(--line);font-size:15px}
.llx-steps li:before{content:counter(s);position:absolute;left:0;top:9px;width:28px;height:28px;border-radius:50%;background:var(--acc,var(--green));color:#fff;display:grid;place-items:center;font:700 13px var(--sans)}
.llx-meta{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:16px;font-size:14px}.llx-meta b{display:block;font:500 11px var(--mono);text-transform:uppercase;letter-spacing:.08em;color:var(--mute);margin-bottom:6px}
.llx-guard{background:var(--tint,var(--paper2));border-radius:12px;padding:12px 14px;font-size:14px;margin-top:14px}
.llx-cta{display:flex;gap:10px;flex-wrap:wrap;margin-top:18px}
.llx-other{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-top:22px}
.llx-other a{display:block;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px;text-decoration:none;font-size:13.5px}.llx-other a b{display:block;font-size:14.5px}
.llx-pf{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-top:24px}
.llx-pf div{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px}.llx-pf i{font-style:normal;font-size:20px}.llx-pf b{display:block;font:600 16px var(--serif);margin:8px 0 4px}.llx-pf p{font-size:14px;color:var(--ink2)}
.llx-tm{margin-top:24px;border:1px solid #D9D6CC;border-radius:14px;overflow:hidden;background:#fff;font-family:var(--mono);box-shadow:0 24px 50px -30px rgba(0,0,0,.35)}
.llx-tmh{display:flex;justify-content:space-between;align-items:center;padding:9px 14px;background:#F7F7F4;border-bottom:1px solid #E5E2D8;font-size:12px;color:#5E5A50}
.llx-tmh b{color:#B85F00;letter-spacing:.08em}
.llx-tmg{display:grid;grid-template-columns:1.1fr 1fr;min-height:340px}
.llx-wire{border-right:1px solid #E5E2D8}
.llx-wi{display:grid;grid-template-columns:78px 1fr;gap:10px;width:100%;text-align:left;background:none;border:0;border-bottom:1px solid #EFEDE6;padding:12px 14px;cursor:pointer;font:inherit;color:#1C1B18}
.llx-wi.on{background:#FFF4E2;box-shadow:inset 3px 0 0 #B85F00}
.llx-wi em{font-style:normal;font-size:10.5px;font-weight:600;color:#8A4700;background:#FFF1DC;border-radius:4px;padding:3px 5px;align-self:start;text-align:center}
.llx-wi span{font:600 13.5px var(--sans)}.llx-wi small{display:block;font-size:11px;color:#8A867A;margin-top:3px}
.llx-pan{padding:16px 18px;font-family:var(--sans);font-size:14.5px}
.llx-pan .go{margin-top:12px;background:#B85F00;color:#fff;border:0;border-radius:8px;padding:10px 14px;font:600 14px var(--sans);cursor:pointer}
.llx-pan h5{font:600 12px var(--mono);letter-spacing:.06em;text-transform:uppercase;color:#8A4700;margin:14px 0 6px}
.llx-pan ul{padding-left:18px}.llx-pan li{margin:4px 0}
.llx-pan .pl{display:block;margin-top:12px;padding:9px 11px;border:1px solid #B85F00;border-radius:8px;background:#FFF1DC;color:#8A4700;font-weight:600;text-decoration:none;cursor:pointer}
.llx-note{font:500 11px var(--mono);color:#8A867A;padding:8px 14px;border-top:1px solid #EFEDE6}
.llx-home{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin-top:22px}
.llx-home a{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:20px;text-decoration:none;cursor:pointer}.llx-home a b{display:block;font:600 18px var(--serif);margin:6px 0}
.llx-home a small{font:500 11px var(--mono);color:var(--acc,var(--green2));text-transform:uppercase;letter-spacing:.08em}
@media(max-width:900px){.llx-pb,.llx-tmg,.llx-meta{grid-template-columns:1fr}.llx-pf,.llx-other{grid-template-columns:1fr 1fr}.llx-home{grid-template-columns:1fr}.llx-wire{border-right:0;border-bottom:1px solid #E5E2D8}}
@media(max-width:560px){.llx-pf,.llx-other{grid-template-columns:1fr}}
</style>"""

SECTIONS = """<section class="wrap llx-sec" id="playbooks">
  <div class="eyebrow">Profit playbooks</div><h2>Turn every alert into money</h2>
  <p class="lead">Specific ways %(who)s profit from the data, each with a step-by-step guide using the portal and the terminal. The same playbooks are inside your account.</p>
  %(ind)s<div class="llx-pb"><div class="llx-tabs" id="pbTabs"></div><div class="llx-body" id="pbBody"></div></div>%(other)s
</section>
<section class="wrap llx-sec" id="platform">
  <div class="eyebrow">The platform</div><h2>One platform behind every %(site)s plan</h2>
  <p class="lead">Your feed is one part of it. Every account includes the portal, Company Insight, the watchlist, the playbooks and the market terminal. Higher plans add volume, seats, CRM sync and file drops.</p>
  <div class="llx-pf" id="pfGrid"></div>
  <div class="llx-cta"><a class="btn btn-p" id="llxPortal" target="_blank" rel="noopener">Open the portal ↗</a><a class="btn btn-g" id="llxTerm" target="_blank" rel="noopener">Open the market terminal ↗</a></div>
</section>
<section class="wrap llx-sec" id="terminal">
  <div class="eyebrow">Market terminal</div><h2>The news, and what it means for you</h2>
  <p class="lead">A live wire of company signals and sector news, linked to the companies you watch. Pick any item and the AI analyst explains what it means and how to profit from it. Try the preview.</p>
  <div class="llx-tm"><div class="llx-tmh"><b>LEDGERLIGHT TERMINAL</b><span id="tmClock"></span></div>
  <div class="llx-tmg"><div class="llx-wire" id="tmWire"></div><div class="llx-pan" id="tmPan"></div></div>
  <div class="llx-note">Preview · invented sample headlines. In the live terminal you choose the view: your company, customers, contractors, creditors or opportunity.</div></div>
</section>
"""

JS = r"""<script id="llx-js">
(function(){const D=window.LLX,$=s=>document.querySelector(s),e=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
let ind=D.brands[0].id,cur=null,w=0,open=false;
const B=()=>D.brands.find(b=>b.id===ind);
function pb(){const ib=$('#pbInd');if(ib)ib.innerHTML=D.brands.map(b=>`<button class="llx-chip ${b.id===ind?'on':''}" data-ind="${b.id}">${e(b.name)} · ${e(b.industry)}</button>`).join('');
const ps=B().plays;if(!ps.some(p=>p.id===cur))cur=ps[0].id;
$('#pbTabs').innerHTML=ps.map((p,i)=>`<button class="llx-tab ${p.id===cur?'on':''}" data-play="${p.id}"><span>0${i+1}</span>${e(p.title)}</button>`).join('');
const p=ps.find(x=>x.id===cur);
$('#pbBody').innerHTML=`<h3>${e(p.title)}</h3><p class="llx-profit">${e(p.profit)}</p><p class="llx-money">${e(p.money)}</p><div class="llx-sig">${p.signals.map(s=>`<span>${e(s)}</span>`).join('')}</div><h4>Step by step</h4><ol class="llx-steps">${p.steps.map(s=>`<li>${e(s)}</li>`).join('')}</ol><div class="llx-meta"><div><b>Tools used</b>${p.features.map(f=>`<span class="llx-f">${e(D.features[f])}</span>`).join('')}</div><div><b>What to track</b>${e(p.track)}</div></div><div class="llx-guard"><b>Guardrails:</b> ${e(p.guardrails)}</div><div class="llx-cta"><a class="btn btn-p" href="${D.app}/app?brand=${ind}&signup=1&play=${p.id}" target="_blank" rel="noopener">Start this play: 14-day free trial ↗</a><a class="btn btn-g" href="#terminal">See it in the terminal</a></div>`}
function pf(){$('#pfGrid').innerHTML=D.platform.map(f=>`<div><i>${f[0]}</i><b>${e(f[1])}</b><p>${e(f[2])}</p></div>`).join('');$('#llxPortal').href=`${D.app}/app?brand=${D.brand}&signup=1`;$('#llxTerm').href=`${D.app}/terminal`}
function tm(){$('#tmWire').innerHTML=D.wire.map((x,i)=>`<button class="llx-wi ${i===w?'on':''}" data-wire="${i}"><em>${e(x[0])}</em><div><span>${e(x[1])}</span><small>${e(x[2])}</small></div></button>`).join('');
const x=D.wire[w],pl=D.plays[x[5]];
$('#tmPan').innerHTML=`<div style="font:600 11px var(--mono);color:#8A4700">${e(x[0])} · ${e(x[2])}</div><div style="font:600 18px var(--serif);margin:6px 0 4px">${e(x[1])}</div>`+(open?`<h5>What it means</h5><p>${e(x[3])}</p><h5>What to do</h5><ul>${x[4].map(a=>`<li>${e(a)}</li>`).join('')}</ul>${pl?`<a class="pl" data-goplay="${x[5]}">Playbook: ${e(pl.title)} →</a>`:''}`:`<button class="go" data-explain="1">What this means for you</button>`)}
document.addEventListener('click',ev=>{const t=ev.target.closest('[data-ind],[data-play],[data-wire],[data-explain],[data-goplay]');if(!t)return;
if(t.dataset.ind){ind=t.dataset.ind;cur=null;pb()}else if(t.dataset.play){cur=t.dataset.play;pb()}else if(t.dataset.wire){w=+t.dataset.wire;open=false;tm()}else if(t.dataset.explain){open=true;tm()}
else if(t.dataset.goplay){const id=t.dataset.goplay,b=D.brands.find(b=>b.plays.some(p=>p.id===id));if(b){ind=b.id;cur=id;pb()}location.hash='playbooks';setTimeout(()=>{const s=document.getElementById('playbooks');if(s)s.scrollIntoView()},50)}});
const hm=$('#pbHome');if(hm)hm.innerHTML=D.brands.slice(0,6).map(b=>`<a data-goplay="${b.plays[0].id}"><small>${e(b.name)}</small><b>${e(b.plays[0].title)}</b><span class="muted" style="font-size:14px">${e(b.plays[0].profit)}</span></a>`).join('');
pb();pf();tm();setInterval(()=>{const c=$('#tmClock');if(c)c.textContent=new Date().toLocaleTimeString('en-GB')},1000);})();
</script>"""


def data_for(brand, pb):
    if brand == "ledgerlight":
        order = list(pb["brands"])
        wire = [WIRE[b][0] for b in order]
    else:
        order = [brand]
        wire = WIRE[brand]
    brands = [{"id": b, "name": pb["brands"][b]["name"], "industry": pb["brands"][b]["industry"],
               "plays": pb["brands"][b]["plays"]} for b in order]
    plays = {p["id"]: {"title": p["title"]} for b in brands for p in b["plays"]}
    return {"app": APP, "brand": brand, "brands": brands, "features": pb["features"], "platform": PLATFORM,
            "wire": wire, "plays": plays}


def build(brand, pb):
    d = data_for(brand, pb)
    if brand == "ledgerlight":
        sections = SECTIONS % {"who": "every industry can", "site": "Ledgerlight",
                               "ind": '<div class="llx-chips" id="pbInd"></div>', "other": ""}
        # The master site shows one view per hash (#playbooks, #platform, #terminal): wrap each section in a view.
        parts = sections.split("<section ")[1:]
        sections = "".join(f'<div class="view" id="v-{part.split(chr(34) + " id=" + chr(34), 1)[1].split(chr(34))[0]}">'
                           f'<section {part.rstrip()}</div>\n' for part in parts)
    else:
        others = "".join(f'<a href="{SITES[b]}" target="_blank" rel="noopener"><b>{v["name"]} ↗</b>{v["industry"]}</a>'
                         for b, v in pb["brands"].items() if b != brand)
        sections = SECTIONS % {"who": (lambda s: s[0].lower() + s[1:])(pb["brands"][brand]["industry"]) + " firms",
                               "site": pb["brands"][brand]["name"], "ind": "",
                               "other": f'<h3 style="margin-top:34px">Playbooks for other industries</h3><div class="llx-other">{others}</div>'}
    script = "<script>window.LLX=" + json.dumps(d, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/") + ";</script>\n" + JS
    return CSS, sections, script


def main():
    pb = json.loads((ROOT / "data" / "playbooks.json").read_text(encoding="utf-8"))
    out = ROOT / "build" / "site_blocks"
    out.mkdir(parents=True, exist_ok=True)
    for brand in SITES:
        css, html, js = build(brand, pb)
        (out / f"{brand}.css").write_text(css, encoding="utf-8")
        (out / f"{brand}.html").write_text(html, encoding="utf-8")
        (out / f"{brand}.js").write_text(js, encoding="utf-8")
        print(brand, len(css) + len(html) + len(js))


if __name__ == "__main__":
    main()
