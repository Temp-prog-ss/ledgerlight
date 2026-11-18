"""The master platform and its eight vertical brands.

Every event is processed once, then routed to each brand whose `interest`
function returns a relevance score. Brands differ in what they watch, how they
score relevance, which extra fields they expose and how strict their
compliance rules are.
"""
from dataclasses import dataclass, field
from typing import Callable, Optional

# Canonical event types produced by the data collectors.
INSOLVENCY = {"winding_up_petition", "compulsory_liquidation", "administration", "cvl",
              "cva", "restructuring_plan", "receivership", "moratorium", "liquidator_appointed"}
DISTRESS = {"accounts_overdue", "cs_overdue", "strike_off_proposed", "new_charge", "ccj",
            "director_resigned", "phoenix_pattern", "disqualified_link"}
GROWTH = {"incorporated", "id_verification_due", "threshold_crossed"}
SPONSOR = {"sponsor_added", "sponsor_removed", "sponsor_rating_change"}
ESTATE = {"deceased_estate", "unclaimed_estate", "land_owned_by_dissolved"}
CHANGE = {"psc_change", "ro_change", "director_appointed", "dissolved", "charge_satisfied"}
ALL_TYPES = INSOLVENCY | DISTRESS | GROWTH | SPONSOR | ESTATE | CHANGE

CONSTRUCTION_SIC = ("41", "42", "43", "711", "7112", "77320")
PROPERTY_SIC = ("68",)
CARE_SIC = ("86", "87", "88")


@dataclass
class Brand:
    id: str
    name: str
    tagline: str
    site_url: str
    event_types: set
    interest: Callable  # (event_type, company: dict, on_watchlist: bool) -> Optional[int]
    vetted_only: bool = False          # only vetted orgs may see events (probate)
    allows_personal_data: bool = False  # may show notices about individuals (e.g. deceased estates)
    watchlist_only_types: set = field(default_factory=set)  # only routed if on a customer's watchlist
    accent: str = "#0F4D3A"


def _sic(company, prefixes):
    codes = (company or {}).get("sic_codes") or ""
    return any(c.strip().startswith(prefixes) for c in codes.split(",") if c.strip())


def _base(event_type):
    return {"winding_up_petition": 80, "administration": 85, "compulsory_liquidation": 80, "cvl": 75,
            "cva": 70, "restructuring_plan": 75, "receivership": 70, "moratorium": 65, "liquidator_appointed": 70,
            "ccj": 55, "accounts_overdue": 45, "cs_overdue": 40, "strike_off_proposed": 55,
            "new_charge": 35, "director_resigned": 30, "phoenix_pattern": 70, "disqualified_link": 75}.get(event_type, 40)


def _master(t, c, w):
    return _base(t) + (10 if w else 0)


def _construction(t, c, w):
    if t not in INSOLVENCY | DISTRESS:
        return None
    if not (w or _sic(c, CONSTRUCTION_SIC)):
        return None
    return min(100, _base(t) + (15 if w else 0) + (10 if _sic(c, CONSTRUCTION_SIC) else 0))


def _sponsor(t, c, w):
    is_sponsor = bool((c or {}).get("sponsor"))
    if t in SPONSOR:
        return 90 if t == "sponsor_removed" else 70
    if is_sponsor and t in INSOLVENCY | {"strike_off_proposed", "psc_change", "dissolved"}:
        return min(100, _base(t) + 15 + (10 if _sic(c, CARE_SIC) else 0))
    return None


def _estate(t, c, w):
    if t == "deceased_estate":
        return 70
    if t == "unclaimed_estate":
        return 65
    if t == "land_owned_by_dissolved":
        return 60 + min(30, 5 * int((c or {}).get("titles_owned") or 0))
    return None


def _distress(t, c, w):
    if t in {"administration", "cvl", "compulsory_liquidation", "liquidator_appointed"}:
        return 50  # competitor appointments: lower relevance
    if t in DISTRESS | {"winding_up_petition"}:
        turnover = (c or {}).get("turnover") or 0
        return min(100, _base(t) + (10 if turnover >= 1_000_000 else 0) + (10 if w else 0))
    return None


def _tenant(t, c, w):
    titles = int((c or {}).get("titles_owned") or 0)
    if t == "land_owned_by_dissolved":
        return 70
    if t in INSOLVENCY and (w or titles > 0 or _sic(c, PROPERTY_SIC)):
        return min(100, _base(t) + (20 if w else 0) + (10 if titles else 0))
    if w and t in DISTRESS:
        return _base(t) + 10
    return None


def _talent(t, c, w):
    staff = int((c or {}).get("employees") or 0)
    if t in {"administration", "cvl", "compulsory_liquidation"} and staff >= 20:
        return min(100, 60 + min(35, staff // 10))
    if w and t in INSOLVENCY | {"ccj", "winding_up_petition"}:
        return _base(t) + 10  # a client that owes the agency fees
    return None


def _credit(t, c, w):
    if not w:
        return None
    if t in INSOLVENCY | DISTRESS | {"psc_change", "ro_change", "dissolved"}:
        return min(100, _base(t) + 15)
    return None


def _filing(t, c, w):
    if t == "incorporated":
        return 60
    if t in {"accounts_overdue", "cs_overdue", "id_verification_due", "threshold_crossed"}:
        return 65
    if t == "strike_off_proposed":
        return 75
    return None


BRANDS = {
    "ledgerlight": Brand("ledgerlight", "Ledgerlight", "UK company event intelligence",
                         "https://claude.ai/artifact/GQWgFUXpdo5uVGt89ooMGb", ALL_TYPES - {"deceased_estate", "unclaimed_estate"}, _master),
    "sitesentinel": Brand("sitesentinel", "SiteSentinel", "Construction & building services intelligence",
                          "https://claude.ai/artifact/NdL4NRozMoK4kH95C86VRD", INSOLVENCY | DISTRESS, _construction, accent="#C2410C"),
    "sponsorwatch": Brand("sponsorwatch", "SponsorWatch", "Sponsor licence & immigration intelligence",
                          "https://claude.ai/artifact/Y5sbbDbpT5hmc7F6m6YAhW", SPONSOR | INSOLVENCY | {"strike_off_proposed", "psc_change", "dissolved"}, _sponsor, accent="#1D4ED8"),
    "estatesignal": Brand("estatesignal", "EstateSignal", "Probate & inheritance intelligence",
                          "https://claude.ai/artifact/Xnjpe31KgXc5HZzQFq1DWX", ESTATE, _estate, vetted_only=True, allows_personal_data=True, accent="#7C3A6E"),
    "distressradar": Brand("distressradar", "DistressRadar", "Distress intelligence for insolvency practitioners",
                           "https://claude.ai/artifact/T6PH8DceswoZUThyBjPQct", INSOLVENCY | DISTRESS, _distress, accent="#B91C1C"),
    "tenantguard": Brand("tenantguard", "TenantGuard", "Tenant & property intelligence",
                         "https://claude.ai/artifact/1PRcChcX6ncbtUmbsRkxva", INSOLVENCY | DISTRESS | {"land_owned_by_dissolved"}, _tenant, accent="#0F766E"),
    "talentrelease": Brand("talentrelease", "TalentRelease", "Talent intelligence for recruiters",
                           "https://claude.ai/artifact/M2MKVLCZsKYQYTmnuwo1Lf", INSOLVENCY | {"ccj"}, _talent, accent="#6D28D9"),
    "creditpulse": Brand("creditpulse", "CreditPulse", "Customer credit monitoring",
                         "https://claude.ai/artifact/SikDycyXemmqBg41KNcZYx", INSOLVENCY | DISTRESS | CHANGE, _credit,
                         watchlist_only_types=INSOLVENCY | DISTRESS | CHANGE, accent="#047857"),
    "filingpulse": Brand("filingpulse", "FilingPulse", "New business & compliance leads for accountants",
                         "https://claude.ai/artifact/NJ5XepbM9oZv1yRgwX5sm1", GROWTH | {"accounts_overdue", "cs_overdue", "strike_off_proposed"}, _filing, accent="#4338CA"),
}


def get(brand_id) -> Optional[Brand]:
    return BRANDS.get(brand_id)


def relevance(brand_id, event_type, company, on_watchlist=False):
    b = BRANDS[brand_id]
    if event_type not in b.event_types:
        return None
    if event_type in b.watchlist_only_types and not on_watchlist:
        return None
    return b.interest(event_type, company or {}, on_watchlist)
