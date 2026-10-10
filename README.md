# Scout

[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

Two tools in one repo:

| | **Enrichment** (`enrich_cli.py`) | **Scouting** (`scout.py`) |
|---|---|---|
| Job | Given a named contact (SPOC), find and **verify** their email and phone from multiple public sources | Find leads: scrape public profiles and discover businesses |
| For | Agent / pipeline use: JSON in, JSON out | Interactive use: terminal menu, CSV out |
| Code | `app/enrichment/` | `app/scouting/` |

Enrichment never imports scouting, so it runs on its own. Shared helpers live in `app/common/`.

> Forked from [kiryano/Scout](https://github.com/kiryano/Scout) (MIT).

## Install

```bash
git clone https://github.com/Tanv1n1/ScoutLeads.git
cd ScoutLeads
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # then add your keys
```

The enrichment tool needs only `httpx` and `dnspython`; `rich` is only for the scouting menu.

## Enrichment: complete and verify a SPOC's contact details

```bash
python enrich_cli.py spocs.json out.json --batch 10 --workers 3
```

Re-run the same command until it prints `"remaining": 0`. Progress is saved after every record (in `<output>.state.json`), so it is safe to stop and resume, and short enough for agent sessions that kill long scripts.

| Flag | Default | Purpose |
|---|---|---|
| `--batch N` | 10 | Max SPOCs per run |
| `--workers N` | 3 | SPOCs processed in parallel |
| `--no-smtp` | off | Skip the local MX/SMTP check (e.g. port 25 blocked) |
| `--erp FILE` | off | Also write ERP `leadContacts` rows to FILE (see below) |
| `--erp-min-status S` | `VERIFIED` | Lowest `email_status` written to `workEmail` (`LIKELY`, `SOURCE_CONFIRMED` or `VERIFIED`) |

**Input**: a JSON list. `name` and `domain` (or `website`) are required; a record without a domain is not guessed and comes back with the gap `domain_unresolved`. `company` and `role` are echoed back. Anything you already have (`email`, `phone`) is kept and checked.

```json
[{"lead_id": "L-102", "name": "Priya Nair", "role": "CEO", "company": "Acme Digital",
  "domain": "acme.in", "email": "", "phone": ""}]
```

**Output**: the same SPOC, completed.

| Field | Meaning |
|---|---|
| `email`, `email_status`, `email_confidence` | The best verified or source-backed address. Empty if none passed. |
| `email_sources`, `email_verification` | Where it was found, and the MX / SMTP / catch-all / API verdicts. |
| `phone`, `phone_scope`, `phone_sources` | `person` (next to their name), `company` (switchboard) or `provided`. |
| `other_candidates`, `company_inboxes` | Runner-up addresses; role inboxes (`info@`, `sales@`) as a fallback only. |
| `gaps` | Everything still missing, e.g. `email_not_found`, `email_unverified`, `phone_not_found`, `domain_unresolved`. A gap is a data gap, never a rejection. |
| `sources_checked`, `provenance`, `enriched_at` | Audit trail. |

**`email_status`**

| Status | Meaning | Use as |
|---|---|---|
| `VERIFIED` | ZeroBounce says valid, or the mail server accepted it and the domain is not catch-all | send-ready |
| `SOURCE_CONFIRMED` | Cannot be server-verified, but two or more independent sources list it for this person | usable |
| `LIKELY` | One public source lists it for this person | usable, flag it |
| `UNVERIFIED` / `NOT_FOUND` | Only guesses, or nothing | do not send |

Addresses the server rejects, or on domains with no mail server, are never returned. A guess is never presented as found.

### ERP output (`leadContacts`)

With `--erp rows.json`, Scout also writes one row per SPOC in the ERP's `leadContacts` shape (camelCase field names), ready to upsert on `contactId`. Scout only fills contact fields; `leadEnrichments` (savings, rails, pitch) is out of its scope.

| ERP field | Filled from |
|---|---|
| `contactId` | input `contact_id`, else `<lead_id>__<name-slug>` (stable, so re-runs upsert the same row) |
| `name`, `fullName` | input `name` |
| `designation` | input `role` (or `designation`) |
| `din`, `linkedinUrl` | input `din`, `linkedin_url`, passed through; Scout never looks these up |
| `workEmail` | found email, **only if** `email_status` is at least `--erp-min-status` (default `VERIFIED`) |
| `phoneNumber` | found phone, **only if** it is the person's own number (`phone_scope` `person`) or was provided; a company switchboard is not written |
| `dataProvenance` | input `data_provenance` (e.g. `mca_statutory_filing`), else `public_web_sources` |
| `consentBasis` | input `consent_basis`, else `legitimate-interest-b2b` |
| `isPrimary` | input `is_primary`, else `true` |
| `leadId` | input `lead_id`; it must be the `leadMaster` record id, because it becomes the relation's join column |

Empty values are left out of the row rather than sent as null, so an upsert never blanks a field already in the ERP. The full result in `out.json` still holds the status, confidence, sources and gaps for every contact, including emails below the threshold. To store several contacts for one lead, give each its own `contact_id`.

### Sources and verification

| Step | What it does | Needs |
|---|---|---|
| Provided | Keeps and re-checks values you pass in | — |
| Company site | Fixed team/contact paths, plus team/leadership/about links found on the homepage and in `sitemap.xml`; reads `mailto:`/`tel:` links and text next to the name | — |
| Structured data | schema.org `Person` / `Organization` (email, telephone, job title) | — |
| Web search | Serper or Tavily (first key found; falls back to the other on failure), then fetches the result pages (social networks are never fetched). Cached 7 days. | `SERPER_API_KEY` and/or `TAVILY_API_KEY` |
| Hunter | Email finder | `HUNTER_API_KEY` |
| Pattern guess | Builds `first.last@` etc. from patterns seen on the domain; accepted only if confirmed | — |
| Verify | MX + SMTP with a catch-all probe (free), then **ZeroBounce** for every address that survives | `ZEROBOUNCE_API_KEY` (recommended) |

SMTP checks need outbound port 25. With a ZeroBounce key, port 25 being blocked no longer matters: ZeroBounce does the deliverability check from its own servers (pass `--no-smtp` to skip the local check). It costs one ZeroBounce credit per address checked; addresses SMTP already proves undeliverable are skipped, results are cached for 7 days, and checking of further pattern guesses stops once one is confirmed.

A ZeroBounce verdict of `valid` gives `VERIFIED`; `invalid`, `spamtrap`, `abuse` and `do_not_mail` are rejected and never returned; `catch-all` and `unknown` stay `LIKELY` / `SOURCE_CONFIRMED`. The raw verdict and sub-status are kept in `email_verification.api_detail`. This predicts a bounce, it cannot guarantee delivery.

### Configuration

All optional; the tool runs with none, with fewer sources.

| Variable | Purpose |
|---|---|
| `SERPER_API_KEY` | Web search; 2,500 free queries at signup, no card |
| `TAVILY_API_KEY` | Web search; 1,000 free credits a month, no card |
| `ZEROBOUNCE_API_KEY` | Primary email verifier |
| `HUNTER_API_KEY` | Email finder |
| `SCOUT_SMTP_HELO`, `SCOUT_SMTP_FROM` | Identity used in SMTP checks (use a domain you control) |
| `SCOUT_CACHE_DIR`, `SCOUT_CACHE=0` | Cache location (default `.scout_cache`); disable caching |
| `SCOUT_PROXY` | Single egress proxy for outbound requests |
| `SCOUT_DELAY_MIN`, `SCOUT_DELAY_MAX` | Scouting menu: seconds between scrapes |
| `LINKEDIN_COOKIE` | Scouting menu: LinkedIn `li_at` cookie |

`enrich_cli.py` reads real environment variables; export them or load your `.env` first (`set -a; source .env; set +a`). An agent skill file for pipelines is in [skills/scout-company-enrichment/SKILL.md](skills/scout-company-enrichment/SKILL.md).

## Scouting: find leads (interactive)

```bash
python scout.py            # menu;  --verbose for debug logs,  --version,  --help
```

| Menu | What it does |
|---|---|
| Platforms 1–8 | Instagram, TikTok, LinkedIn (session cookie), GitHub, YouTube, Twitch, Linktree and similar, Pinterest: public profile, bio, followers, email and phone found in the bio |
| 9 Bulk scrape | The same, from a CSV/TXT list of usernames |
| 12 Discover | Searches for businesses and scores them on cross-border signals (worldwide shipping, export language, multi-currency) and recurring-billing signals (subscription pricing, Stripe/Paddle/Chargebee/Payoneer/Wise). Ranks by fit score. Needs `SERPER_API_KEY` or `TAVILY_API_KEY`, or paste domains. |
| 10 / 11 | View past exports / settings |

After scraping, an optional enrichment step runs each lead through the same engine as `enrich_cli.py` (company site, search, Hunter, SMTP + ZeroBounce verification). A lead needs a company website in their profile (link-in-bio pages don't count); without one the result carries the gap `domain_unresolved`. The CSV gains `email_status`, `email_confidence`, `email_sources`, `phone_scope` and `enrichment_gaps` columns. Results export to `<platform>_export_<timestamp>.csv`. Discovery signals are keyword evidence, not proof.

LinkedIn needs your `li_at` cookie (browser DevTools → Application → Cookies) as `LINKEDIN_COOKIE` in `.env`. Proxies are optional. The menu reads `.env` itself; `enrich_cli.py` does not.

## Tests

```bash
pip install pytest
python -m pytest tests
```

The tests run offline.

## Responsible use

Scraping personal profiles can breach platform terms and data-protection law (for example India's DPDP Act 2023, or GDPR). Prefer company-level, publicly published business contacts, record where each contact came from (`email_sources`, `provenance`), and honour opt-outs. The enrichment tool does not scrape social networks.

## License

MIT, see [LICENSE](LICENSE).
