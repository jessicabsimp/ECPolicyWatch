# ECPolicyWatch — Data Refresh Guide

This document explains how to run the data refresh script.

## What it does

1. Searches Open States for 20 ECE-related keywords across Tennessee (session 114)
   and U.S. Congress (session 119)
2. Deduplicates results across keyword searches
3. Parses TN bill titles into subject + plain-language summary + amendment clause
4. Writes three output files to the repo root:
   - `bills.json` — compressed list-view data (fast initial load)
   - `bills-detail.json` — rich per-bill modal data (lazy-loaded)
   - `meta.json` — tag + sponsor index

## One-time setup

**Get an Open States API key** at https://openstates.org/accounts/profile/ (free).

```bash
cp .env.example .env
# Edit .env and paste in your OPENSTATES_API_KEY
```

No pip installs needed — script uses only Python 3 standard library (3.8+).

## Running a refresh

```bash
OPENSTATES_API_KEY=$(grep OPENSTATES_API_KEY .env | cut -d= -f2) \
  python3 scripts/refresh_data.py
```

**Dry run** (no files written):
```bash
OPENSTATES_API_KEY=your_key python3 scripts/refresh_data.py --dry-run
```

**Expected runtime:** 15–25 minutes (the script pauses 12s between API calls
to respect Open States' 10 req/min rate limit).

## After a successful run

```bash
git add bills.json bills-detail.json meta.json
git commit -m "Refresh data ($(date +%Y-%m-%d))"
git push
```

Cloudflare auto-deploys on push to `main`.

## GitHub Actions (automatic weekly)

The workflow at `.github/workflows/refresh.yml` runs every Monday at 9 AM ET.

To enable:
1. Repo → **Settings → Secrets → Actions**
2. Add secret: `OPENSTATES_API_KEY` = your key value

You can also trigger a manual run from the Actions tab.

## Adding a new session

When a new session starts, update `CURRENT_SESSIONS` and `SESSION_DISPLAY`
in `scripts/refresh_data.py`, then run a fresh refresh.
