---
name: scout-company-enrichment
description: Complete a named SPOC's missing email and phone from multiple public web sources and verify the email. Use when a lead's SPOC record from the primary sources lacks a verified email or a phone number.
---

# Scout SPOC contact completion

Pass the SPOC the primary sources returned; Scout fills only what is missing and re-checks what is given.

```bash
python enrich_cli.py spocs.json out.json --batch 10 --workers 3   # re-run until "remaining": 0
python enrich_cli.py spocs.json out.json --batch 10 --no-smtp # if port 25 is blocked on the host
```

Input per SPOC: `{"lead_id","name","role","company","domain","email","phone"}` (name and domain or website are required; without a domain the gap `domain_unresolved` is returned).

## Sources it checks (in order)
provided values; the company site (team/leadership/about links found on the homepage and sitemap, plus schema.org data); web search (`SERPER_API_KEY` and/or `TAVILY_API_KEY`; first available, falls back to the other); Hunter (`HUNTER_API_KEY`); then pattern guesses confirmed by the mail server or a verifier. Verifiers: SMTP (free), then ZeroBounce (`ZEROBOUNCE_API_KEY`, one credit per address). No social-network scraping.

## Reading the result
| `email_status` | Meaning | Use as |
|---|---|---|
| `VERIFIED` | ZeroBounce says valid, or the mail server accepted it and the domain is not catch-all | send-ready |
| `SOURCE_CONFIRMED` | Server cannot confirm, but 2+ independent sources list it for this person | usable |
| `LIKELY` | One public source lists it for this person; not server-verified | usable, flag |
| `UNVERIFIED` / `NOT_FOUND` | Only guesses, or nothing | do not send; see `other_candidates`, `gaps` |

- `REJECTED` addresses (server refused, no MX) are never returned.
- `phone_scope`: `person` (next to the name), `company` (switchboard), `provided`.
- `gaps` lists everything still missing (`email_not_found`, `email_unverified`, `phone_not_found`, `web_search_skipped_no_search_key`, `domain_unresolved`). A gap is a data gap, never a rejection of the lead.
- `company_inboxes` (info@, sales@...) are a fallback, not the SPOC.
- Always carry `email_sources` and `provenance` into the ERP row. Personal data is subject to DPDP Act 2023.
