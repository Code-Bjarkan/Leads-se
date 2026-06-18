import os
import re
import sys
import time
import unicodedata
import requests
from dotenv import load_dotenv

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

load_dotenv()

HUBSPOT_TOKEN   = os.environ["HUBSPOT_TOKEN"]
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")

LEADS_SE_CHANNEL  = "C04LKBNNRFD"
BOT_ID            = "B01J5UBRSUE"
DEMO_HEADER       = "Ny demo request från hemsidan"
FOAT_HEADER       = "Ny intresseanmälan FoAT"

# Set to True to post suggestions without real @-mentions (safe for testing)
SUGGEST_ONLY = False

SALES_SWE_GROUP_ID = "S0B3EP26P4L"  # @sales-swe
CS_SWE_GROUP_ID    = "S09LZN0TMLL"  # @cs_swe

# Email domain → kommun fallback (exact matches)
EMAIL_DOMAIN_KOMMUN = {
    "skola.botkyrka.se":  "Botkyrka",
    "edu.linkoping.se":   "Linköping",
    "edu.avesta.se":      "Avesta",
    "edu.vilhelmina.se":  "Vilhelmina",
    "karlstad.edu.se":    "Karlstad",
    # Plain kommunnamn.se domains
    "varberg.se":         "Varberg",
    "stockholm.se":       "Stockholm",
    "goteborg.se":        "Göteborg",
    "malmo.se":           "Malmö",
    "uppsala.se":         "Uppsala",
    "vasteras.se":        "Västerås",
    "orebro.se":          "Örebro",
    "linkoping.se":       "Linköping",
    "helsingborg.se":     "Helsingborg",
    "jonkoping.se":       "Jönköping",
    "norrkoping.se":      "Norrköping",
    "umea.se":            "Umeå",
    "lund.se":            "Lund",
    "boras.se":           "Borås",
    "gavle.se":           "Gävle",
    "sundsvall.se":       "Sundsvall",
    "eskilstuna.se":      "Eskilstuna",
    "sodertalje.se":      "Södertälje",
    "karlstad.se":        "Karlstad",
    "vastmanland.se":     "Västerås",
    "huddinge.se":        "Huddinge",
    "nacka.se":           "Nacka",
    "sollentuna.se":      "Sollentuna",
    "haninge.se":         "Haninge",
    "tyreso.se":          "Tyresö",
    "jarfalla.se":        "Järfälla",
    "botkyrka.se":        "Botkyrka",
    "danderyd.se":        "Danderyd",
    "lidingo.se":         "Lidingö",
    "vaxholm.se":         "Vaxholm",
    "osteraker.se":       "Österåker",
    "vallentuna.se":      "Vallentuna",
    "sigtuna.se":         "Sigtuna",
    "upplands-vasby.se":  "Upplands Väsby",
    "upplandsbro.se":     "Upplands-Bro",
    "enkoping.se":        "Enköping",
    "halmstad.se":        "Halmstad",
    "vaxjo.se":           "Växjö",
    "kalmar.se":          "Kalmar",
    "kristianstad.se":    "Kristianstad",
    "ostersund.se":       "Östersund",
    "lulea.se":           "Luleå",
    "gavleborg.se":       "Gävle",
}

LAST_SEEN_FILE = os.path.join(os.path.dirname(__file__), "last_seen.txt")

# ---------------------------------------------------------------------------
# HubSpot helpers
# ---------------------------------------------------------------------------

HS_HEADERS = {"Authorization": f"Bearer {HUBSPOT_TOKEN}"}


def hs_get_owners():
    r = requests.get("https://api.hubapi.com/crm/v3/owners?limit=200", headers=HS_HEADERS)
    r.raise_for_status()
    return {o["id"]: o for o in r.json()["results"]}


def hs_search_company(name):
    body = {
        "filterGroups": [{"filters": [{"propertyName": "name", "operator": "CONTAINS_TOKEN", "value": name}]}],
        "properties": ["name", "csm", "hubspot_owner_id", "lifecyclestage", "country"],
        "limit": 10,
    }
    r = requests.post(
        "https://api.hubapi.com/crm/v3/objects/companies/search",
        headers=HS_HEADERS,
        json=body,
    )
    r.raise_for_status()
    results = r.json()["results"]
    # Swedish companies are named "SWE - <NAME>" — sort those first
    def swedish_rank(c):
        return 0 if (c["properties"].get("name") or "").upper().startswith("SWE -") else 1
    return sorted(results, key=swedish_rank)


def resolve_owner(owner_id, owners):
    o = owners.get(owner_id) or owners.get(str(owner_id))
    if not o:
        return None, None
    email = o.get("email", "")
    name  = f"{o.get('firstName', '')} {o.get('lastName', '')}".strip()
    return email, name


def lookup_person(search_term, owners):
    """Return (email, display_name, company_name) for the responsible person.
    Returns (None, None, company_name) if company found but owner is inactive."""
    companies = hs_search_company(search_term)
    if not companies:
        return None, None, None

    matched_company = None
    for c in companies:
        props = c["properties"]
        company_name = props["name"]
        matched_company = company_name

        # Customer: CSM is set — tag the CSM
        csm_id = props.get("csm")
        if csm_id:
            email, name = resolve_owner(csm_id, owners)
            if email:
                return email, name, company_name

        # Lead/opportunity: tag the company owner
        owner_id = props.get("hubspot_owner_id")
        if owner_id:
            email, name = resolve_owner(owner_id, owners)
            if email:
                return email, name, company_name

    return None, None, matched_company


# ---------------------------------------------------------------------------
# Slack helpers
# ---------------------------------------------------------------------------

SL_HEADERS = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}


def slack_get_messages(channel, oldest):
    r = requests.get(
        "https://slack.com/api/conversations.history",
        headers=SL_HEADERS,
        params={"channel": channel, "oldest": oldest, "limit": 50},
    )
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Slack error: {data.get('error')}")
    return data.get("messages", [])


def slack_get_bot_id():
    if not SLACK_BOT_TOKEN:
        return None
    r = requests.get("https://slack.com/api/auth.test", headers=SL_HEADERS)
    return r.json().get("bot_id")


def slack_already_replied(channel, thread_ts, router_bot_id):
    if not router_bot_id:
        return False
    r = requests.get(
        "https://slack.com/api/conversations.replies",
        headers=SL_HEADERS,
        params={"channel": channel, "ts": thread_ts, "limit": 20},
    )
    if not r.ok or not r.json().get("ok"):
        return False
    for msg in r.json().get("messages", [])[1:]:
        if msg.get("bot_id") == router_bot_id:
            return True
    return False


def slack_post_reply(channel, thread_ts, text):
    if not SLACK_BOT_TOKEN:
        print(f"  [DRY-RUN, no SLACK_BOT_TOKEN] Would post: {text}")
        return
    r = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers=SL_HEADERS,
        json={"channel": channel, "thread_ts": thread_ts, "text": text},
    )
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Slack post error: {data.get('error')}")


def normalize(text):
    """Lowercase and strip diacritics so å/ä/ö match a/a/o etc."""
    nfkd = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def slack_load_users():
    """Return (email→id, normalised_name→id, email_prefix→id) maps for all active Slack users."""
    if not SLACK_BOT_TOKEN:
        return {}, {}, {}
    r = requests.get(
        "https://slack.com/api/users.list",
        headers=SL_HEADERS,
        params={"limit": 500},
    )
    by_email, by_name, by_prefix = {}, {}, {}
    for u in r.json().get("members", []):
        if u.get("deleted") or u.get("is_bot"):
            continue
        profile = u.get("profile", {})
        email = profile.get("email", "").lower()
        if email:
            by_email[email] = u["id"]
            prefix = email.split("@")[0]
            if prefix:
                by_prefix[prefix] = u["id"]
        name = normalize(u.get("real_name", "").strip())
        if name:
            by_name[name] = u["id"]
    return by_email, by_name, by_prefix


def slack_lookup_user(hs_email, hs_name, slack_by_email, slack_by_name, slack_by_prefix):
    """Find Slack user ID: exact email → email prefix (handles magma.se↔magmamath.com) → name."""
    uid = slack_by_email.get(hs_email.lower())
    if uid:
        return uid
    prefix = hs_email.split("@")[0].lower()
    uid = slack_by_prefix.get(prefix)
    if uid:
        return uid
    return slack_by_name.get(normalize(hs_name))


def mention(slack_uid, name, email, suggest_only):
    if suggest_only:
        return f"{name} ({email})"
    if slack_uid:
        return f"<@{slack_uid}>"
    return f"{name} ({email})"


def sales_swe_mention(suggest_only):
    if suggest_only or not SALES_SWE_GROUP_ID:
        return "@sales-swe"
    return f"<!subteam^{SALES_SWE_GROUP_ID}>"


def cs_sweden_mention(suggest_only):
    if suggest_only or not CS_SWE_GROUP_ID:
        return "@cs_swe"
    return f"<!subteam^{CS_SWE_GROUP_ID}>"


# ---------------------------------------------------------------------------
# Message parsing
# ---------------------------------------------------------------------------

MUNICIPAL_SUFFIX_RE = re.compile(r"\s+(kommun|stad|landsting|region)\s*$", re.IGNORECASE)


def strip_markup(text):
    text = re.sub(r"<mailto:[^|]+\|([^>]+)>", r"\1", text)
    text = re.sub(r"<tel:[^|]+\|([^>]+)>", r"\1", text)
    return text


# Keep old name as alias so nothing else breaks
strip_mailto = strip_markup


def extract_lines(message):
    lines = message.get("text", "").split("\n")
    result = []
    for l in lines:
        l = strip_markup(l.strip())
        if not l:
            continue
        if "zapier.com" in l.lower():
            continue
        result.append(l)
    return result


def clean_search_term(term):
    """Strip Swedish municipal suffixes so 'Östhammar kommun' → 'Östhammar'."""
    return MUNICIPAL_SUFFIX_RE.sub("", term).strip()


def parse_demo(lines):
    # [0]=timestamp [1]=first [2]=last [3]=email [4]=kommun [5]=phone [6]=role [7]=source [8]=school_type
    return {
        "type":    "demo",
        "email":   lines[3] if len(lines) > 3 else "",
        "kommun":  lines[4] if len(lines) > 4 else "",
    }


def parse_foat(lines):
    # [0]=timestamp [1]=name [2]=email [3]=source/municipality [4]=school
    return {
        "type":     "foat",
        "email":    lines[2] if len(lines) > 2 else "",
        "municipality": lines[3] if len(lines) > 3 else "",
        "school":   lines[4] if len(lines) > 4 else "",
    }


# Labels that are never a kommunname — skip when extracting candidates dynamically
_GENERIC_LABELS = {
    "edu", "skola", "skolor", "utbildning", "mail", "smtp", "mx",
    "kommune", "kommun", "stad", "region", "landsting",
    "www", "e", "m", "k12",
}


def email_domain_candidates(email):
    """Return ordered list of search terms to try against HubSpot from the email domain.

    Strategy:
    1. Static table entry (handles diacritics / unusual structure).
    2. Dynamic: strip TLD, filter out generic labels, capitalize each remaining
       label as a HubSpot search candidate. Works for any TLD.

    Examples:
      lena@varberg.se          → ["Varberg"]
      per@skola.varberg.se     → ["Varberg"]
      x@malmo.kommune.no       → ["Malmo"]   (→ HubSpot finds Malmö)
      x@edu.linkoping.se       → ["Linköping", "Linkoping"]  (static first)
      x@halmstad.se            → ["Halmstad"]
    """
    domain = email.split("@", 1)[1].lower() if "@" in email else ""
    if not domain:
        return []
    candidates = []
    static = EMAIL_DOMAIN_KOMMUN.get(domain)
    if static:
        candidates.append(static)
    # Dynamic: all labels except TLD, minus generic words
    parts = domain.split(".")
    seen = {c.lower() for c in candidates}
    for label in parts[:-1]:  # drop TLD
        if label in _GENERIC_LABELS:
            continue
        dynamic = label.capitalize()
        if dynamic.lower() not in seen:
            candidates.append(dynamic)
            seen.add(dynamic.lower())
    return candidates


# Keep for backward compat with any callers
def email_to_kommun(email):
    cands = email_domain_candidates(email)
    return cands[0] if cands else ""


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def build_reply(parsed, owners, slack_by_email, slack_by_name, slack_by_prefix):
    lead_email = parsed.get("email", "")

    if parsed["type"] == "demo":
        kommun = parsed.get("kommun", "").strip()
        if not kommun:
            candidates = email_domain_candidates(lead_email)
            if candidates:
                domain = lead_email.split("@")[-1] if "@" in lead_email else ""
                print(f"  [fallback] Kommun empty → email domain '{domain}' → trying {candidates}")
                kommun = candidates[0]  # best candidate; the no-match branch will try the rest
        if not kommun:
            return f"Ingen kommun angiven — taggat {sales_swe_mention(SUGGEST_ONLY)}."
        search_term  = clean_search_term(kommun)
        source_label = f"kommun: {kommun}"

    else:  # foat
        municipality = parsed.get("municipality", "").strip()
        school = parsed.get("school", "").strip()
        if municipality:
            search_term  = clean_search_term(municipality)
            source_label = f"kommun: {municipality}"
        elif school:
            search_term  = clean_search_term(school)
            source_label = f"skola: {school}"
        else:
            return f"Ingen kommun eller skola angiven — taggat {cs_sweden_mention(SUGGEST_ONLY)}."

    email, name, company = lookup_person(search_term, owners)

    if not email:
        # Kommun was present but didn't match HubSpot — try email-domain fallback
        if parsed["type"] == "demo":
            domain = lead_email.split("@")[-1] if "@" in lead_email else ""
            candidates = [c for c in email_domain_candidates(lead_email)
                          if c.lower() != search_term.lower()]
            if candidates:
                for candidate in candidates:
                    print(f"  [fallback] Tried {source_label} → no match → "
                          f"email domain '{domain}' → trying '{candidate}'")
                    fb_email, fb_name, fb_company = lookup_person(clean_search_term(candidate), owners)
                    if fb_email:
                        uid = slack_lookup_user(fb_email, fb_name, slack_by_email, slack_by_name, slack_by_prefix)
                        tag = mention(uid, fb_name, fb_email, SUGGEST_ONLY)
                        label = "Föreslagen ägare" if SUGGEST_ONLY else "Ägare"
                        print(f"  [fallback] Matched '{fb_company}' via email domain fallback")
                        return f"{label}: {tag} (HubSpot: {fb_company}) [matchad via e-postdomän {domain}]"
                    print(f"  [fallback] '{candidate}' → no HubSpot match")
            elif domain:
                print(f"  [fallback] Tried {source_label} → no match → "
                      f"could not extract kandidat from '{domain}'")
            return f"Ingen matchning för {source_label} — taggat {sales_swe_mention(SUGGEST_ONLY)}."
        return f"Ingen matchning för {source_label} — taggat {cs_sweden_mention(SUGGEST_ONLY)}."

    uid   = slack_lookup_user(email, name, slack_by_email, slack_by_name, slack_by_prefix)
    tag   = mention(uid, name, email, SUGGEST_ONLY)
    label = "Föreslagen ägare" if SUGGEST_ONLY else "Ägare"
    return f"{label}: {tag} (HubSpot: {company})"


# ---------------------------------------------------------------------------
# Poll loop
# ---------------------------------------------------------------------------

def load_last_seen():
    try:
        raw = open(LAST_SEEN_FILE, encoding="utf-8-sig").read().strip()
        return str(int(float(raw)))
    except FileNotFoundError:
        return str(int(time.time() - 3600))


def save_last_seen(ts):
    with open(LAST_SEEN_FILE, "w") as f:
        f.write(str(int(float(ts))))


def run_once(owners, slack_by_email, slack_by_name, slack_by_prefix, router_bot_id):
    oldest   = load_last_seen()
    messages = slack_get_messages(LEADS_SE_CHANNEL, oldest)
    new_ts   = oldest

    for msg in reversed(messages):
        ts = msg.get("ts", "")
        if ts <= oldest:
            continue
        if ts > new_ts:
            new_ts = ts

        if msg.get("bot_id") != BOT_ID:
            continue

        if slack_already_replied(LEADS_SE_CHANNEL, ts, router_bot_id):
            print(f"[{ts}] Already replied — skipping.")
            continue

        lines = extract_lines(msg)
        if not lines:
            continue

        header = lines[0]
        if DEMO_HEADER in header:
            parsed = parse_demo(lines[1:])
        elif FOAT_HEADER in header:
            parsed = parse_foat(lines[1:])
        else:
            continue

        reply = build_reply(parsed, owners, slack_by_email, slack_by_name, slack_by_prefix)
        print(f"[{ts}] {header[:50]}")
        print(f"  -> {reply}")
        slack_post_reply(LEADS_SE_CHANNEL, ts, reply)

    save_last_seen(new_ts)


def main():
    if not SLACK_BOT_TOKEN:
        print("WARNING: SLACK_BOT_TOKEN not set — will print replies but not post them.")

    if SUGGEST_ONLY:
        print("Running in SUGGEST-ONLY mode (no real @-mentions).")

    owners = hs_get_owners()
    print(f"Loaded {len(owners)} HubSpot owners.")

    slack_by_email, slack_by_name, slack_by_prefix = slack_load_users()
    print(f"Loaded {len(slack_by_email)} Slack users ({len(slack_by_prefix)} email prefixes).")

    router_bot_id = slack_get_bot_id()
    print(f"Router bot ID: {router_bot_id}")

    if "--once" in sys.argv:
        run_once(owners, slack_by_email, slack_by_name, slack_by_prefix, router_bot_id)
    else:
        print("Polling #leads-se every 60 s. Ctrl+C to stop.")
        while True:
            try:
                run_once(owners, slack_by_email, slack_by_name, slack_by_prefix, router_bot_id)
            except Exception as e:
                print(f"Error: {e}")
            time.sleep(60)


if __name__ == "__main__":
    main()
