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

## Enrichment: complete and verify a SPOC's contact details

```bash
python enrich_cli.py spocs.json out.json --batch 10 --workers 3
```

Re-run the same command until it prints `"remaining": 0`. Progress is saved after every record, so it is safe to stop and resume, and short enough for agent sessions that kill long scripts.

**Input**: a JSON list. `name` plus `company` or `domain` is enough; anything you already have is kept and checked.

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
| `VERIFIED` | Mail server accepted it and the domain is not catch-all, or a verifier API says valid | send-ready |
| `SOURCE_CONFIRMED` | Cannot be server-verified, but two or more independent sources list it for this person | usable |
| `LIKELY` | One public source lists it for this person | usable, flag it |
| `UNVERIFIED` / `NOT_FOUND` | Only guesses, or nothing | do not send |

Addresses the server rejects, or on domains with no mail server, are never returned. A guess is never presented as found.

### Sources and verification

| Step | What it does | Needs |
|---|---|---|
| Provided | Keeps and re-checks values you pass in | — |
| Company site | Fixed team/contact paths, plus team/leadership/about links found on the homepage and in `sitemap.xml`; reads `mailto:`/`tel:` links and text next to the name | — |
| Structured data | schema.org `Person` / `Organization` (email, telephone, job title) | — |
| Web search | Brave and/or Serper results, then fetches the result pages (social networks are never fetched). Cached 7 days. | `BRAVE_API_KEY` and/or `SERPER_API_KEY` |
| Hunter | Email finder | `HUNTER_API_KEY` |
| Pattern guess | Builds `first.last@` etc. from patterns seen on the domain; accepted only if confirmed | — |
| Verify | MX + SMTP with a catch-all probe; Hunter and ZeroBounce are asked when SMTP cannot decide | optional `HUNTER_API_KEY`, `ZEROBOUNCE_API_KEY` |

SMTP checks need outbound port 25. If your host blocks it, SMTP results come back `unknown`; set a Hunter or ZeroBounce key (or pass `--no-smtp`) and expect more `LIKELY` than `VERIFIED`.

### Configuration

All optional; the tool runs with none, with fewer sources.

| Variable | Purpose |
|---|---|
| `BRAVE_API_KEY`, `SERPER_API_KEY` | Web search backends (second is a fallback) |
| `HUNTER_API_KEY` | Email finder and verifier |
| `ZEROBOUNCE_API_KEY` | Verifier |
| `SCOUT_SMTP_HELO`, `SCOUT_SMTP_FROM` | Identity used in SMTP checks (use a domain you control) |
| `SCOUT_CACHE_DIR`, `SCOUT_CACHE=0` | Cache location (default `.scout_cache`); disable caching |
| `SCOUT_PROXY`, `SCOUT_PROXY_FILE`, `SCOUT_FREE_PROXY` | Proxy for outbound requests |

`enrich_cli.py` reads real environment variables; export them or load your `.env` first (`set -a; source .env; set +a`). An agent skill file for pipelines is in [skills/scout-company-enrichment/SKILL.md](skills/scout-company-enrichment/SKILL.md).

## Scouting: find leads (interactive)

```bash
python scout.py            # menu;  --verbose for debug logs,  --version,  --help
```

| Menu | What it does |
|---|---|
| Platforms 1–8 | Instagram, TikTok, LinkedIn (session cookie), GitHub, YouTube, Twitch, Linktree and similar, Pinterest: public profile, bio, followers, email and phone found in the bio |
| 9 Bulk scrape | The same, from a CSV/TXT list of usernames |
| 12 Discover | Searches for businesses and scores them on cross-border signals (worldwide shipping, export language, multi-currency) and recurring-billing signals (subscription pricing, Stripe/Paddle/Chargebee/Payoneer/Wise). Ranks by fit score. Needs `BRAVE_API_KEY`, or paste domains. |
| 10 / 11 | View past exports / settings |

After scraping, an optional enrichment step looks for the lead's email on their site and bio links, detects the email pattern, and verifies by SMTP. Results export to `<platform>_export_<timestamp>.csv`. Discovery signals are keyword evidence, not proof.

LinkedIn needs your `li_at` cookie (browser DevTools → Application → Cookies) as `LINKEDIN_COOKIE` in `.env`. Proxies are optional.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest tests
```

The tests run offline.

## Responsible use

Scraping personal profiles can breach platform terms and data-protection law (for example India's DPDP Act 2023, or GDPR). Prefer company-level, publicly published business contacts, record where each contact came from (`email_sources`, `provenance`), and honour opt-outs. The enrichment tool does not scrape social networks.

## License

MIT, see [LICENSE](LICENSE).
