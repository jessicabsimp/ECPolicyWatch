#!/usr/bin/env python3
"""
ECPolicyWatch — Data Refresh Pipeline
=====================================

Fetches ECE-related bills from Open States API and writes:

    bills.json         list-view data (compressed, fast initial load)
    bills-detail.json  per-bill rich data (description, sponsors, actions)
    meta.json          tag + sponsor index, counts

Usage
-----
    # Set key in .env or environment, then:
    OPENSTATES_API_KEY=... python3 scripts/refresh_data.py

    # Dry-run (print what would be written, skip disk writes):
    OPENSTATES_API_KEY=... python3 scripts/refresh_data.py --dry-run

Get a key at: https://openstates.org/accounts/profile/ (free)

Design notes
------------
• Uses keyword searches rather than paginating all session bills.
  This keeps request count manageable (~80–120 per full run).
• Rate limit: Open States free tier allows 10 req/min.
  We use REQUEST_DELAY=12s between calls to stay safely under.
• A full run takes roughly 15–25 minutes.
• Deduplicates across keyword searches by bill identifier.
• Sessions are filtered CLIENT-SIDE (Open States session param is unreliable
  for the US jurisdiction). Bills from older sessions are excluded automatically.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import urllib.request
import urllib.parse
import urllib.error

# ── Paths ───────────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_BILLS = REPO_ROOT / "bills.json"
OUT_DETAIL = REPO_ROOT / "bills-detail.json"
OUT_META = REPO_ROOT / "meta.json"

# ── API settings ─────────────────────────────────────────────────────────────

OPENSTATES_BASE = "https://v3.openstates.org"
USER_AGENT = "ECPolicyWatch/1.0 (+https://ecpolicywatch.org)"
PER_PAGE = 20           # max per_page for Open States free tier
REQUEST_DELAY = 12.0    # seconds between requests (10 req/min limit → 12s = safe)
MAX_PAGES_PER_KEYWORD = 8  # cap per search term to prevent quota blowout

# ── Sessions we actively track ────────────────────────────────────────────────
# Open States returns these as short strings ("114", "119").
# Bills from other sessions are silently skipped.

CURRENT_SESSIONS = {
    "tn": {"114"},
    "us": {"119"},
}

SESSION_DISPLAY = {
    ("tn", "114"): "114th General Assembly",
    ("tn", "113"): "113th General Assembly",
    ("us", "119"): "119th Congress",
    ("us", "118"): "118th Congress",
}

# ── ECE keyword searches ──────────────────────────────────────────────────────
# Each entry triggers a separate API search. Results are deduplicated by
# bill identifier. Shorter / more specific terms are more efficient.

ECE_SEARCH_KEYWORDS = [
    # Childcare / early education
    "child care",
    "preschool",
    "head start",
    "early childhood",
    "pre-k",
    "home visiting",
    "early intervention",
    "kindergarten",
    # Parental / family support
    "foster care",
    "child welfare",
    "child tax credit",
    "paid leave",
    "maternal",
    # Child hunger / nutrition
    "school nutrition",
    "school meals",
    "summer ebt",
    "food assistance",
    # Broader ECE umbrella
    "child development",
    "childcare subsidy",
    "child care workforce",
]

# ── Tag mapping ───────────────────────────────────────────────────────────────
# Maps substrings found in bill titles to our canonical tag vocabulary.
# Longer / more specific patterns should come first.

TAG_MAP = [
    ("child care workforce", "Workforce"),
    ("childcare subsidy", "Subsidy"),
    ("child care subsidy", "Subsidy"),
    ("pre-k", "Pre-K"), ("prekindergarten", "Pre-K"),
    ("preschool", "Pre-K"),
    ("kindergarten", "Kindergarten"),
    ("head start", "Head Start"),
    ("child care", "Child Care"), ("childcare", "Childcare"),
    ("day care", "Child Care"), ("daycare", "Child Care"),
    ("home visiting", "Home Visiting"),
    ("early childhood", "Early Childhood"),
    ("early learning", "Early Learning"),
    ("early intervention", "Early Intervention"),
    ("child development", "Child Development"),
    ("literacy", "Literacy"),
    ("workforce", "Workforce"),
    ("licensing", "Licensing"),
    ("subsidy", "Subsidy"),
    ("foster care", "Foster Care"),
    ("adoption", "Adoption"),
    ("child welfare", "Child Welfare"),
    ("trauma", "Trauma-Informed Care"),
    ("safety", "Safety"),
    ("child abuse", "Child Welfare"),
    ("child protective", "Child Welfare"),
    ("family leave", "Family Leave"),
    ("paid leave", "Paid Leave"),
    ("parental leave", "Family Leave"),
    ("maternal", "Maternal Health"),
    ("dependent care", "Dependent Care"),
    ("child tax credit", "Child Tax Credit"),
    ("tax credit", "Tax Credit"),
    ("school nutrition", "School Nutrition"),
    ("school meals", "School Nutrition"),
    ("school lunch", "School Nutrition"),
    ("school breakfast", "School Nutrition"),
    ("child nutrition", "School Nutrition"),
    ("summer ebt", "Summer EBT"),
    ("food assistance", "Food Assistance"),
    ("usda", "USDA"),
    ("wic ", "Food Assistance"),
]


# ── Logging ────────────────────────────────────────────────────────────────────

def log(msg: str, level: str = "INFO") -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] [{level}] {msg}", flush=True)


# ── Open States client ─────────────────────────────────────────────────────────

class OpenStatesClient:
    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise RuntimeError(
                "OPENSTATES_API_KEY not set. "
                "Get a key at https://openstates.org/accounts/profile/ "
                "and add it to your .env file."
            )
        self.api_key = api_key
        self.requests_made = 0
        self._last_request = 0.0

    def _get(self, path: str, params: dict) -> dict:
        # Respect rate limit: sleep if needed
        elapsed = time.monotonic() - self._last_request
        if elapsed < REQUEST_DELAY and self.requests_made > 0:
            time.sleep(REQUEST_DELAY - elapsed)

        encoded = []
        for k, v in params.items():
            if isinstance(v, list):
                for item in v:
                    encoded.append((k, item))
            else:
                encoded.append((k, v))

        qs = urllib.parse.urlencode(encoded)
        url = f"{OPENSTATES_BASE}{path}?{qs}"

        req = urllib.request.Request(
            url,
            headers={
                "X-API-KEY": self.api_key,
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            },
        )

        self._last_request = time.monotonic()
        self.requests_made += 1

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")[:400]
            raise RuntimeError(f"HTTP {e.code} from Open States: {body}") from e

    def search_bills(
        self,
        jurisdiction: str,
        keyword: str,
        includes: list[str] | None = None,
    ) -> list[dict]:
        """Search for bills matching `keyword` in `jurisdiction`, paginated.
        Stops early once we've fetched MAX_PAGES_PER_KEYWORD pages."""
        if includes is None:
            includes = ["sponsorships", "actions"]

        all_bills: list[dict] = []
        page = 1
        params_base = {
            "jurisdiction": jurisdiction,
            "q": keyword,
            "per_page": PER_PAGE,
            "include": includes,
        }

        while page <= MAX_PAGES_PER_KEYWORD:
            params = dict(params_base, page=page)
            resp = self._get("/bills", params)
            results = resp.get("results", [])
            pagination = resp.get("pagination", {})

            all_bills.extend(results)
            max_page = pagination.get("max_page", 1)

            if page >= max_page or not results:
                break
            page += 1

        return all_bills


# ── Title / summary parsing ────────────────────────────────────────────────────

def parse_tn_title(title: str) -> tuple[str, str, str]:
    """
    TN bills from Open States embed three components in the title field,
    separated by ' - ':
        <Subject Category> - <Plain-language summary> - <Amends TCA …>

    Returns (subject, summary, amends_clause).
    Falls back to (title, title, '') if the structure isn't recognized.
    """
    if not title or " - " not in title:
        return title or "", title or "", ""

    parts = [p.strip() for p in title.split(" - ")]
    if len(parts) >= 3:
        subject = parts[0]
        amends = parts[-1] if parts[-1].lower().startswith("amends") else ""
        summary = " - ".join(parts[1:-1] if amends else parts[1:])
        # Strip "As enacted, " / "As introduced, " prefix
        for prefix in ("As enacted, ", "As introduced, ", "As amended, "):
            if summary.startswith(prefix):
                summary = summary[len(prefix):]
                break
        return subject.strip(), summary.strip(), amends.strip()

    if len(parts) == 2:
        return parts[0], parts[1], ""

    return title, title, ""


def extract_summary(jurisdiction: str, raw: dict) -> tuple[str, str, str]:
    """Return (subject, summary, amends) for a bill."""
    title = raw.get("title") or ""
    # Try explicit abstracts first (sometimes present for US bills)
    for a in raw.get("abstracts") or []:
        abstract = (a.get("abstract") or "").strip()
        if abstract and abstract.lower() != title.lower():
            return "", abstract, ""
    # TN: parse the embedded structure
    if jurisdiction == "tn":
        return parse_tn_title(title)
    # Federal: title IS the summary (short and readable for current session)
    return "", title, ""


# ── Tag extraction ─────────────────────────────────────────────────────────────

def extract_tags(title: str) -> list[str]:
    tl = title.lower()
    seen: dict[str, bool] = {}
    tags: list[str] = []
    for substring, tag in TAG_MAP:
        if substring in tl and tag not in seen:
            seen[tag] = True
            tags.append(tag)
    return tags or ["ECE-Related"]


# ── Status ────────────────────────────────────────────────────────────────────

def derive_status(raw: dict) -> str:
    desc = (raw.get("latest_action_description") or "").lower()
    if not desc:
        return "Status not available"
    if "signed by governor" in desc or "pub. ch." in desc or "became law" in desc:
        return "Signed by Governor."
    if "enacted" in desc and ("passed" in desc or "signed" in desc):
        return "Signed by Governor."
    if ("enrolled" in desc or
            ("passed senate" in desc and "passed house" in desc) or
            "passed both" in desc):
        return "Passed Both Chambers"
    if "passed house" in desc or "passed senate" in desc:
        return "Passed First Chamber"
    if "vetoed" in desc:
        return "Vetoed"
    if any(k in desc for k in ("filed", "introduced", "intro.", "p1c", "p2c", "read first")):
        return "Filed"
    # Use the raw description, capped at 80 chars, as a last resort
    return desc.strip()[:80]


# ── Normalize ────────────────────────────────────────────────────────────────

def normalize_bill_number(identifier: str) -> str:
    """'HB 6' → 'HB0006', 'SJR 1103' → 'SJR1103'."""
    if not identifier:
        return ""
    parts = identifier.replace(".", "").split()
    if len(parts) == 2:
        prefix, num = parts
        if num.isdigit():
            return f"{prefix}{int(num):04d}"
    return identifier.replace(" ", "")


def normalize_sponsors(raw: dict) -> tuple[list[dict], list[dict]]:
    primary: list[dict] = []
    co: list[dict] = []
    for s in raw.get("sponsorships") or []:
        record = {"name": s.get("name") or "", "party": s.get("party") or "", "district": ""}
        if (s.get("classification") or "").lower() == "primary":
            primary.append(record)
        else:
            co.append(record)
    return primary, co


def normalize_actions(raw: dict) -> list[dict]:
    actions = []
    for a in raw.get("actions") or []:
        actions.append({
            "date": (a.get("date") or "")[:10],
            "description": (a.get("description") or "").strip(),
        })
    actions.sort(key=lambda a: a.get("date") or "", reverse=True)
    return actions


def normalize_bill(jurisdiction: str, raw: dict) -> dict | None:
    identifier = raw.get("identifier") or ""
    bill_number = normalize_bill_number(identifier)
    if not bill_number:
        return None

    origin = "TN" if jurisdiction == "tn" else "Federal"
    session_id = raw.get("session") or ""

    # Skip bills from sessions we don't track
    current = CURRENT_SESSIONS.get(jurisdiction, set())
    if current and session_id not in current:
        return None

    session_display = SESSION_DISPLAY.get((jurisdiction, session_id), session_id)
    title = raw.get("title") or ""
    subject, summary, amends = extract_summary(jurisdiction, raw)
    primary, cosp = normalize_sponsors(raw)
    actions = normalize_actions(raw)

    return {
        "id": f"{origin}-{bill_number}",
        "origin": origin,
        "session": session_display,
        "bill_number": bill_number,
        "title": title,
        "subject": subject,
        "summary": summary,
        "amends": amends,
        "tags": extract_tags(title),
        "status": derive_status(raw),
        "sponsors": primary,
        "co_sponsors": cosp,
        "actions": actions,
        "last_updated": (raw.get("latest_action_date") or "")[:10],
        "url": raw.get("openstates_url") or "",
    }


# ── Output builders ────────────────────────────────────────────────────────────

def build_outputs(bills: list[dict]) -> tuple[dict, dict, dict]:
    """
    bills.json (compressed list view):
        s  — [unique session strings]
        st — [unique status strings]
        sm — [unique summary strings]
        b  — [ [origin, sIdx, bill_number, title, [tags], stIdx, url, last_updated, smIdx], ... ]

    bills-detail.json (rich per-bill modal data):
        { "<bill_id>": { title, summary, subject, amends, sponsors, co_sponsors, actions } }

    meta.json:
        { tags, sponsors, tn_count, fed_count, total, refreshed_at }
    """

    def idx_of(lst: list, val: str) -> int:
        if not val:
            return 0
        if val in lst:
            return lst.index(val)
        lst.append(val)
        return len(lst) - 1

    sessions: list[str] = [""]
    statuses: list[str] = [""]
    summaries: list[str] = [""]

    rows = []
    detail: dict = {}
    tag_set: set[str] = set()
    sponsor_set: set[str] = set()

    for b in bills:
        s_idx = idx_of(sessions, b["session"])
        st_idx = idx_of(statuses, b["status"])
        # Don't dedupe summary when it equals title (saves index space)
        sm = b["summary"] if b["summary"] and b["summary"] != b["title"] else ""
        sm_idx = idx_of(summaries, sm)

        rows.append([
            b["origin"],
            s_idx,
            b["bill_number"],
            b["title"],
            b["tags"],
            st_idx,
            b["url"],
            b["last_updated"],
            sm_idx,          # NEW — index into summaries[]
        ])

        for t in b["tags"]:
            tag_set.add(t)
        for s in b["sponsors"]:
            if s.get("name"):
                sponsor_set.add(s["name"])
        for s in b["co_sponsors"]:
            if s.get("name"):
                sponsor_set.add(s["name"])

        detail[b["id"]] = {
            "title": b["title"],
            "summary": b["summary"],
            "subject": b["subject"],
            "amends": b["amends"],
            "sponsors": b["sponsors"],
            "co_sponsors": b["co_sponsors"],
            "actions": b["actions"][:30],
        }

    bills_compressed = {
        "s": sessions,
        "st": statuses,
        "sm": summaries,
        "b": rows,
    }

    meta = {
        "tags": sorted(tag_set),
        "sponsors": sorted(sponsor_set),
        "tn_count": sum(1 for r in rows if r[0] == "TN"),
        "fed_count": sum(1 for r in rows if r[0] == "Federal"),
        "total": len(rows),
        "refreshed_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }

    return bills_compressed, detail, meta


# ── Main ──────────────────────────────────────────────────────────────────────

def main(dry_run: bool = False) -> int:
    api_key = os.environ.get("OPENSTATES_API_KEY", "").strip()
    if not api_key:
        log("OPENSTATES_API_KEY is not set.", "ERROR")
        log("  Get a key: https://openstates.org/accounts/profile/")
        log("  Then: OPENSTATES_API_KEY=your_key python3 scripts/refresh_data.py")
        return 1

    client = OpenStatesClient(api_key)

    log("=" * 60)
    log("ECPolicyWatch data refresh — Open States v3")
    log(f"  Dry run: {dry_run}")
    log(f"  Request delay: {REQUEST_DELAY}s  |  Max pages/keyword: {MAX_PAGES_PER_KEYWORD}")
    log("=" * 60)

    # Fetch and deduplicate across all keyword searches
    seen_ids: set[str] = set()
    all_normalized: list[dict] = []

    for jurisdiction, jlabel in [("tn", "Tennessee"), ("us", "Federal")]:
        log(f"\n── {jlabel} ──────────────────────────────────────────")
        jur_count = 0

        for keyword in ECE_SEARCH_KEYWORDS:
            log(f'  Searching: "{keyword}"')
            raw_results = client.search_bills(jurisdiction, keyword)

            new = 0
            for raw in raw_results:
                normalized = normalize_bill(jurisdiction, raw)
                if normalized is None:
                    continue
                if normalized["id"] in seen_ids:
                    continue
                seen_ids.add(normalized["id"])
                all_normalized.append(normalized)
                new += 1
                jur_count += 1

            log(f'    → {len(raw_results)} API results, {new} new ECE bills')

        log(f"  {jlabel} total: {jur_count} unique ECE bills")

    log(f"\nGrand total: {len(all_normalized)} ECE bills")
    log(f"  TN:      {sum(1 for b in all_normalized if b['origin'] == 'TN')}")
    log(f"  Federal: {sum(1 for b in all_normalized if b['origin'] == 'Federal')}")
    log(f"  API requests made: {client.requests_made}")

    bills_compressed, detail, meta = build_outputs(all_normalized)

    if dry_run:
        log("\nDry-run mode — no files written.")
        log(f"  Would write bills.json ({len(json.dumps(bills_compressed))} bytes)")
        log(f"  Would write bills-detail.json ({len(json.dumps(detail))} bytes)")
        log(f"  Would write meta.json ({len(json.dumps(meta))} bytes)")
        return 0

    OUT_BILLS.write_text(json.dumps(bills_compressed, separators=(",", ":")))
    OUT_DETAIL.write_text(json.dumps(detail, separators=(",", ":")))
    OUT_META.write_text(json.dumps(meta, separators=(",", ":")))

    log(f"\nWrote bills.json        ({OUT_BILLS.stat().st_size:,} bytes)")
    log(f"Wrote bills-detail.json ({OUT_DETAIL.stat().st_size:,} bytes)")
    log(f"Wrote meta.json         ({OUT_META.stat().st_size:,} bytes)")
    log("\nDone. Commit the updated data files and push to deploy.")
    return 0


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    sys.exit(main(dry_run=dry))
