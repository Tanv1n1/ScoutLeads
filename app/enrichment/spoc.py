"""
SPOC contact completion.

Given a named SPOC (name, company, domain, whatever the caller already has), fill the missing
email / phone from multiple public web sources, verify the email, and return a complete record
with per-field provenance. Never invents a value: unresolved fields stay empty and are listed
in `gaps`.

Sources (each independent, so agreement between them is real evidence):
  provided    what the caller already has (verified, never trusted blindly)
  site        company site: fixed team/contact paths + links found on the homepage + sitemap,
              reading mailto/tel links, schema.org Person/Organization data and text near the name
  web_search  Serper / Tavily / Brave results and the pages they point to (social networks never fetched)
  hunter      Hunter email-finder
  pattern     address built from the domain's observed pattern / common patterns,
              accepted only if the mail server (or a verifier API) confirms it
Verification: MX + SMTP with catch-all probe, then ZeroBounce (Hunter as fallback).
"""

import logging
import os
import re
import time
from typing import Dict, List, Optional
from urllib.parse import urlparse

from app.common.net import random_delay
from app.common.text import emails_in, phones_in, valid_email
from app.enrichment.lead import LeadEnricher, ROLE_LOCALS
from app.enrichment.people import json_ld, local_matches_person, name_parts, page_text
from app.enrichment.sources import search as web
from app.enrichment.sources.site import crawl
from app.enrichment.verify import Verifier, judge

logger = logging.getLogger(__name__)

NO_FETCH_HOSTS = ('linkedin.', 'facebook.', 'instagram.', 'twitter.', 'x.com', 'tiktok.',
                  'naukri.', 'glassdoor.', 'zaubacorp.', 'youtube.')
STATUS_RANK = {'VERIFIED': 4, 'SOURCE_CONFIRMED': 3, 'LIKELY': 2, 'UNVERIFIED': 1}
CONFIDENCE = {'VERIFIED': 95, 'SOURCE_CONFIRMED': 80, 'LIKELY': 60}


class SpocFinder:

    def __init__(self, enricher: Optional[LeadEnricher] = None, smtp: bool = True):
        self.enr = enricher or LeadEnricher(hunter_api_key=os.environ.get('HUNTER_API_KEY') or None)
        self.smtp = smtp
        self._verifier = Verifier(self.enr, smtp)
        self._pages: Dict[str, Dict[str, str]] = {}

    # ---------- fetching ----------
    def site_pages(self, domain: str) -> Dict[str, str]:
        if domain not in self._pages:
            self._pages[domain] = crawl(self.enr._fetch_page, domain)
        return self._pages[domain]

    def web_search(self, query: str, limit: int = 8) -> List[Dict]:
        return web.search(query, limit)

    # ---------- extraction ----------
    def _harvest(self, text: str, source: str, url: str, name: str, domain: str, first: str, last: str,
                 cands: List[Dict], phones: List[Dict]):
        """Pull emails/phones out of text, tagging how strongly each ties to the person."""
        low = text.lower()
        windows = []
        for token in {name.lower(), f'{first} {last}'.strip(), f'{last} {first}'.strip()} - {''}:
            for m in re.finditer(re.escape(token), low):
                windows.append((max(0, m.start() - 150), m.end() + 150))
        near = ' '.join(text[a:b] for a, b in windows)

        for e in emails_in(text):
            if not e.lower().endswith('@' + domain):
                continue
            local = e.split('@')[0]
            if local_matches_person(local, first, last):
                kind = 'person'
            elif e in near and local.lower() not in ROLE_LOCALS and not re.fullmatch(r'[a-z]{2,}[._][a-z]{2,}', local.lower()):
                kind = 'person'          # sits right next to the name and is not a role inbox or someone else's first.last
            else:
                kind = 'role' if local.lower() in ROLE_LOCALS else 'other'
            cands.append({'email': e.lower(), 'source': source, 'url': url, 'kind': kind})

        for p in phones_in(near):
            phones.append({'phone': p, 'scope': 'person', 'source': source, 'url': url})
        if source == 'site':
            for p in phones_in(text)[:2]:
                phones.append({'phone': p, 'scope': 'company', 'source': source, 'url': url})

    def _harvest_schema(self, html: str, url: str, domain: str, first: str, last: str,
                        cands: List[Dict], phones: List[Dict]):
        """schema.org Person / Organization blocks: structured, so a name match is strong evidence."""
        data = json_ld(html)
        for p in data['people']:
            pf, pl = name_parts(p['name'])
            if pf == first and (not last or pl == last):
                if p['email'] and p['email'].endswith('@' + domain) and valid_email(p['email']):
                    cands.append({'email': p['email'], 'source': 'site', 'url': url, 'kind': 'person'})
                for ph in phones_in(p['phone']):
                    phones.append({'phone': ph, 'scope': 'person', 'source': 'site', 'url': url})
        for e in data['org_emails']:
            if e.endswith('@' + domain) and valid_email(e):
                cands.append({'email': e, 'source': 'site', 'url': url,
                              'kind': 'role' if e.split('@')[0] in ROLE_LOCALS else 'other'})
        for ph in data['org_phones']:
            for n in phones_in(ph):
                phones.append({'phone': n, 'scope': 'company', 'source': 'site', 'url': url})

    def _pattern_from(self, others: List[str], domain: str, first: str, last: str) -> List[str]:
        """Pattern(s) seen on other staff emails first, then the common ones."""
        out = []
        for e in others:
            pat = self.enr._detect_pattern(e.split('@')[0].lower())
            if pat and last:
                p = self.enr._apply_pattern(pat, first, last, domain)
                if p and p not in out:
                    out.append(p)
        if first and last:
            for p in [f'{first}.{last}@{domain}', f'{first}@{domain}', f'{first}{last}@{domain}',
                      f'{first[0]}{last}@{domain}', f'{first[0]}.{last}@{domain}', f'{first}.{last[0]}@{domain}']:
                if p not in out:
                    out.append(p)
        return out

    # ---------- verification ----------
    def verify(self, email: str) -> Dict:
        return self._verifier.verify(email)

    @staticmethod
    def _status(v: Dict, n_sources: int, kind: str) -> str:
        verdict = judge(v)
        if verdict == 'invalid':
            return 'REJECTED'
        if verdict == 'valid':
            return 'VERIFIED'
        if kind == 'person' and n_sources >= 2:
            return 'SOURCE_CONFIRMED'            # unverifiable by server, but 2 independent sources show it
        if kind == 'person' and n_sources == 1:
            return 'LIKELY'
        return 'UNVERIFIED'

    # ---------- main ----------
    def find(self, spoc: Dict) -> Dict:
        name = (spoc.get('name') or spoc.get('full_name') or '').strip()
        company = (spoc.get('company') or '').strip()
        domain = self._domain(spoc)
        first, last = name_parts(name)

        out = {'lead_id': spoc.get('lead_id'), 'spoc_name': name, 'role': spoc.get('role', ''), 'company': company,
               'domain': domain or '', 'email': '', 'email_status': 'NOT_FOUND', 'email_confidence': 0,
               'email_sources': [], 'email_verification': None, 'phone': spoc.get('phone', '') or '',
               'phone_scope': 'provided' if spoc.get('phone') else '', 'phone_sources': ['provided'] if spoc.get('phone') else [],
               'other_candidates': [], 'gaps': [], 'sources_checked': [],
               'provenance': 'public_web_sources', 'enriched_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
        if not domain:
            out['gaps'].append('domain_unresolved')
            return out
        if not first:
            out['gaps'].append('spoc_name_missing')

        cands: List[Dict] = []
        phones: List[Dict] = []
        if spoc.get('email'):
            cands.append({'email': spoc['email'].lower(), 'source': 'provided', 'url': '', 'kind': 'person'})

        # site
        pages = self.site_pages(domain)
        out['sources_checked'].append(f'site({len(pages)} pages)')
        for path, page in pages.items():
            url = f'https://{domain}{"" if path == "/" else path}'
            self._harvest(page_text(page), 'site', url, name, domain, first, last, cands, phones)
            self._harvest_schema(page, url, domain, first, last, cands, phones)

        # web search
        if web.available() and first:
            seen_urls = set()
            queries = [f'"{name}" "{company or domain}" email', f'"{name}" "@{domain}"', f'"{name}" {company or domain} phone contact']
            for q in queries:
                for res in self.web_search(q):
                    self._harvest(res['text'], 'web_search', res['url'], name, domain, first, last, cands, phones)
                    host = urlparse(res['url']).netloc.lower()
                    if res['url'] not in seen_urls and len(seen_urls) < 4 and not any(h in host for h in NO_FETCH_HOSTS):
                        seen_urls.add(res['url'])
                        page = self.enr._fetch_page(res['url'])
                        if page:
                            self._harvest(page_text(page), 'web_search', res['url'], name, domain, first, last, cands, phones)
                            self._harvest_schema(page, res['url'], domain, first, last, cands, phones)
                random_delay(0.5, 1.2)
            out['sources_checked'].append(f"web_search[{'+'.join(web.backends())}]({len(seen_urls)} pages)")
        else:
            out['gaps'].append('web_search_skipped_no_search_key' if first else 'web_search_skipped_no_name')

        # hunter
        if self.enr.hunter_api_key and first and last:
            h = self.enr._find_with_hunter(name, domain)
            out['sources_checked'].append('hunter')
            if h and h.lower().endswith('@' + domain):
                cands.append({'email': h.lower(), 'source': 'hunter', 'url': '', 'kind': 'person'})

        # group candidates by address
        grouped: Dict[str, Dict] = {}
        for c in cands:
            g = grouped.setdefault(c['email'], {'email': c['email'], 'sources': set(), 'urls': set(), 'kind': c['kind']})
            g['sources'].add(c['source'])
            if c['url']:
                g['urls'].add(c['url'])
            if c['kind'] == 'person':
                g['kind'] = 'person'

        # pattern guesses only if no person-level candidate exists yet
        if first and last and not any(g['kind'] == 'person' for g in grouped.values()):
            others = [g['email'] for g in grouped.values() if g['kind'] == 'other']
            for p in self._pattern_from(others, domain, first, last)[:6]:
                grouped.setdefault(p, {'email': p, 'sources': {'pattern'}, 'urls': set(), 'kind': 'guess'})
            out['sources_checked'].append('pattern+smtp')

        # verify + rank (person-level candidates and guesses; verify at most 8)
        pool = [g for g in grouped.values() if g['kind'] in ('person', 'guess')][:8]
        ranked = []
        guess_confirmed = False
        for g in pool:
            if g['kind'] == 'guess' and guess_confirmed:
                break                            # one confirmed guess is enough; don't spend more checks
            v = self.verify(g['email'])
            n_indep = len({s for s in g['sources'] if s != 'pattern'})
            status = self._status(v, n_indep, g['kind'])
            if g['kind'] == 'guess' and status not in ('VERIFIED', 'REJECTED'):
                status = 'UNVERIFIED'            # a guess is only usable if the server confirms it
            guess_confirmed = guess_confirmed or (g['kind'] == 'guess' and status == 'VERIFIED')
            ranked.append({**g, 'verification': v, 'status': status, 'sources': sorted(g['sources']), 'urls': sorted(g['urls'])})
        usable = [r for r in ranked if r['status'] != 'REJECTED']
        usable.sort(key=lambda r: (STATUS_RANK.get(r['status'], 0), len(r['sources'])), reverse=True)

        if usable and usable[0]['status'] in ('VERIFIED', 'SOURCE_CONFIRMED', 'LIKELY'):
            b = usable[0]
            out.update(email=b['email'], email_status=b['status'], email_sources=b['sources'],
                       email_verification=b['verification'],
                       email_confidence=CONFIDENCE[b['status']])
            out['other_candidates'] = [{'email': r['email'], 'status': r['status']} for r in usable[1:4]]
        else:
            out['gaps'].append('email_not_found' if not usable else 'email_unverified')
            out['other_candidates'] = [{'email': r['email'], 'status': r['status']} for r in usable[:4]]
        role = sorted({g['email'] for g in grouped.values() if g['kind'] == 'role'})
        if role:
            out['company_inboxes'] = role[:3]     # fallback only; not the SPOC

        # phone: person-level beats company-level; majority wins
        if not out['phone'] and phones:
            person = [p for p in phones if p['scope'] == 'person'] or phones
            best = max({p['phone'] for p in person}, key=lambda x: sum(p['phone'] == x for p in person))
            pick = [p for p in person if p['phone'] == best]
            out.update(phone=best, phone_scope=pick[0]['scope'], phone_sources=sorted({p['source'] for p in pick}))
        if not out['phone']:
            out['gaps'].append('phone_not_found')
        return out

    def _domain(self, spoc: Dict) -> Optional[str]:
        raw = spoc.get('domain') or spoc.get('website') or ''
        return self.enr._extract_domain(raw) if raw else None
