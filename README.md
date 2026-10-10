# Scout

[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

Given a named contact (SPOC) from your primary sources, Scout finds the missing email and phone from multiple public sources, **verifies** the email, and returns the completed record with provenance. JSON in, JSON out, built for agent pipelines.

> Forked from [kiryano/Scout](https://github.com/kiryano/Scout) (MIT); the social-profile scrapers and menu app were removed. They remain in git history before v2.0.0.

## Install

```bash
git clone https://github.com/Tanv1n1/ScoutLeads.git
cd ScoutLeads
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # then add your keys
```

## Usage

```bash
python enrich_cli.py spocs.json out.json --batch 10 --workers 3
```

Re-run the same command until it prints `"remaining": 0`. Progress is saved after every record, so it is safe to stop and resume, and short enough for agent sessions that kill long scripts.

**Input**: a JSON list. `name` plus `domain` (or `website`) is required; without a domain the record returns the gap `domain_unresolved` instead of guessing one. Anything you already have (`email`, `phone`) is kept and checked.

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
| Web search | Brave results, then fetches the result pages (social networks are never fetched). Cached 7 days. | `BRAVE_API_KEY` |
| Hunter | Email finder | `HUNTER_API_KEY` |
| Pattern guess | Builds `first.last@` etc. from patterns seen on the domain; accepted only if confirmed | — |
| Verify | MX + SMTP with a catch-all probe; Hunter is asked when SMTP cannot decide | optional `HUNTER_API_KEY` |

SMTP checks need outbound port 25. If your host blocks it, SMTP results come back `unknown`; set a Hunter key (or pass `--no-smtp`) and expect more `LIKELY` than `VERIFIED`.

### Configuration

All optional; the tool runs with none, with fewer sources.

| Variable | Purpose |
|---|---|
| `BRAVE_API_KEY` | Web search |
| `HUNTER_API_KEY` | Email finder and verifier |
| `SCOUT_SMTP_HELO`, `SCOUT_SMTP_FROM` | Identity used in SMTP checks (use a domain you control) |
| `SCOUT_CACHE_DIR`, `SCOUT_CACHE=0` | Cache location (default `.scout_cache`); disable caching |
| `SCOUT_PROXY` | Single egress proxy for outbound requests |

`enrich_cli.py` reads real environment variables, not `.env`; export them or load it first (`set -a; source .env; set +a`). An agent skill file for pipelines is in [skills/scout-company-enrichment/SKILL.md](skills/scout-company-enrichment/SKILL.md).

## Tests

```bash
pip install pytest
python -m pytest tests
```

The tests run offline.

## Layout

```
enrich_cli.py          batch CLI (resumable, parallel)
app/enrichment/        spoc.py (orchestrator), sources/ (site, search), verify.py, people.py, lead.py (fetch/SMTP/pattern helpers)
app/common/            net.py (user agents, delays, proxy), cache.py, text.py
skills/                agent skill file
```

## Responsible use

Scraping personal profiles can breach platform terms and data-protection law (for example India's DPDP Act 2023, or GDPR). Prefer company-level, publicly published business contacts, record where each contact came from (`email_sources`, `provenance`), and honour opt-outs. Scout does not scrape social networks.

## License

MIT, see [LICENSE](LICENSE).
