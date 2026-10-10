"""
SPOC contact completion.

Given a named SPOC (name, company, domain, whatever the caller already has), fill the missing
email / phone from multiple public web sources, verify the email, and return a complete record
with per-field provenance. Never invents a value: unresolved fields stay empty and are listed
in `gaps`.

Sources, in order (each independent, so agreement between them is real evidence):
  1. provided      - what the caller already has (verified, never trusted blindly)
  2. site          - the company's own team/about/contact pages (mailto, JSON-LD, text near the name)
  3. web_search    - Brave Search results/snippets for the person + company (needs BRAVE_API_KEY);
                     social networks are never fetched
  4. hunter        - Hunter email-finder (needs HUNTER_API_KEY)
  5. pattern       - address built from the domain's observed pattern / common patterns,
                     accepted only if the mail server confirms it
"""

import html as htmllib
import logging
import os
import re
import time
from typing import Dict, List, Optional
from urllib.parse import urlparse

import httpx

from app.scrapers.enrichment import LeadEnricher, ROLE_LOCALS
from app.scrapers.stealth import get_httpx_proxy, random_delay, random_user_agent
from app.scrapers.utils import EMAIL_RE

logger = logging.getLogger(__name__)

TEAM_PATHS = ('', '/contact', '/contact-us', '/about', '/about-us', '/team', '/our-team',
              '/leadership', '/management', '/people', '/company', '/founders')
NO_FETCH_HOSTS = ('linkedin.', 'facebook.', 'instagram.', 'twitter.', 'x.com', 'tiktok.',
                  'naukri.', 'glassdoor.', 'zaubacorp.', 'youtube.')
PHONE_RE = re.compile(r'(?:\+\d{1,3}[\s.-]?)?(?:\(?\d{2,5}\)?[\s.-]?){2,4}\d{2,5}')
STATUS_RANK = {'VERIFIED': 4, 'SOURCE_CONFIRMED': 3, 'LIKELY': 2, 'UNVERIFIED': 1}


def _text(page: str) -> str:
    page = re.sub(r'<(script|style)[^>]*>.*?</\1>', ' ', page, flags=re.S | re.I)
    page = re.sub(r'<a [^>]*href=["\'](?:mailto|tel):([^"\'?]+)[^>]*>', r' \1 ', page, flags=re.I)  # keep mailto/tel targets
    page = re.sub(r'<[^>]+>', ' ', page)
    return re.sub(r'\s+', ' ', htmllib.unescape(page))


def name_parts(full_name: str):
    parts = [p for p in re.sub(r'[^A-Za-z\s.-]', ' ', full_name or '').lower().replace('.', ' ').split() if len(p) > 1 or p.isalpha()]
    parts = [p for p in parts if p not in ('mr', 'ms', 'mrs', 'dr', 'shri', 'smt', 'ca', 'cs')]
    if len(parts) < 2:
        return (parts[0] if parts else ''), ''
    return parts[0], parts[-1]


def local_matches_person(local: str, first: str, last: str) -> bool:
    """Does the local part look like this person's name (not a role inbox, not someone else)?"""
    local = re.sub(r'[^a-z]', '', local.lower().split('+')[0])
    if not first or local in ROLE_LOCALS or not local:
        return False
    if not last:
        return local == first
    forms = {first, first + last, last + first, first[0] + last, first + last[0], last, last + first[0]}
    return local in forms


def phones_in(text: str) -> List[str]:
    out = []
    for m in PHONE_RE.findall(text):
        digits = re.sub(r'\D', '', m)
        if 10 <= len(digits) <= 13 and not re.fullmatch(r'(\d)\1+', digits) and not re.match(r'(19|20)\d{2}', digits):
            norm = ('+' if m.strip().startswith('+') else '') + digits
            if norm not in out:
                out.append(norm)
    return out


class SpocFinder:

    def __init__(self, enricher: Optional[LeadEnricher] = None, smtp: bool = True):
        self.enr = enricher or LeadEnricher(hunter_api_key=os.environ.get('HUNTER_API_KEY') or None)
        self.smtp = smtp
        self._pages: Dict[str, Dict[str, str]] = {}

    # ---------- fetching ----------
    def site_pages(self, domain: str) -> Dict[str, str]:
        if domain not in self._pages:
            pages = {}
            for path in TEAM_PATHS:
                html = self.enr._fetch_page(f'https://{domain}{path}')
                if html:
                    pages[path or '/'] = html
                random_delay(0.15, 0.4)
            self._pages[domain] = pages
        return self._pages[domain]

    def web_search(self, query: str, limit: int = 8) -> List[Dict]:
        key = os.environ.get('BRAVE_API_KEY')
        if not key:
            return []
        try:
            r = httpx.get('https://api.search.brave.com/res/v1/web/search',
                          params={'q': query, 'count': limit},
                          headers={'X-Subscription-Token': key, 'Accept': 'application/json'}, timeout=15)
            r.raise_for_status()
            return [{'url': x['url'], 'text': htmllib.unescape(f"{x.get('title', '')} {x.get('description', '')}")}
                    for x in r.json().get('web', {}).get('results', [])]
        except (httpx.HTTPError, ValueError, KeyError) as e:
            logger.warning(f'search failed: {e}')
            return []

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

        for e in set(re.findall(EMAIL_RE, text)):
            if not self.enr._is_valid_email(e) or not e.lower().endswith('@' + domain):
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
        v = {'mx': False, 'smtp': 'skipped', 'catch_all': None, 'api': None}
        if self.smtp:
            r = self.enr._verify_email_smtp(email)
            v['mx'] = r['mx']
            if not r['mx']:
                v['smtp'] = 'no_mx'
            elif not r['checked']:
                v['smtp'] = 'unknown'           # port 25 blocked / greylisted
            else:
                v['catch_all'] = r['accept_all']
                v['smtp'] = 'accepted' if r['exists'] else 'rejected'
        key = os.environ.get('HUNTER_API_KEY')
        if key and (v['smtp'] in ('unknown', 'skipped') or v['catch_all']):
            try:
                d = httpx.get('https://api.hunter.io/v2/email-verifier', params={'email': email, 'api_key': key},
                              timeout=20).json().get('data', {})
                v['api'] = d.get('status')       # valid | invalid | accept_all | webmail | unknown
            except (httpx.HTTPError, ValueError):
                pass
        return v

    @staticmethod
    def _status(v: Dict, n_sources: int, kind: str) -> str:
        if v['smtp'] in ('rejected', 'no_mx') or v['api'] == 'invalid':
            return 'REJECTED'
        if (v['smtp'] == 'accepted' and not v['catch_all']) or v['api'] == 'valid':
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
            self._harvest(_text(page), 'site', f'https://{domain}{"" if path == "/" else path}', name, domain, first, last, cands, phones)

        # web search
        if os.environ.get('BRAVE_API_KEY') and first:
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
                            self._harvest(_text(page), 'web_search', res['url'], name, domain, first, last, cands, phones)
                random_delay(0.5, 1.2)
            out['sources_checked'].append(f'web_search({len(seen_urls)} pages)')
        else:
            out['gaps'].append('web_search_skipped_no_BRAVE_API_KEY' if first else 'web_search_skipped_no_name')

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
        for g in pool:
            v = self.verify(g['email'])
            n_indep = len({s for s in g['sources'] if s != 'pattern'})
            status = self._status(v, n_indep, g['kind'])
            if g['kind'] == 'guess' and status not in ('VERIFIED', 'REJECTED'):
                status = 'UNVERIFIED'            # a guess is only usable if the server confirms it
            ranked.append({**g, 'verification': v, 'status': status, 'sources': sorted(g['sources']), 'urls': sorted(g['urls'])})
        usable = [r for r in ranked if r['status'] != 'REJECTED']
        usable.sort(key=lambda r: (STATUS_RANK.get(r['status'], 0), len(r['sources'])), reverse=True)

        if usable and usable[0]['status'] in ('VERIFIED', 'SOURCE_CONFIRMED', 'LIKELY'):
            b = usable[0]
            out.update(email=b['email'], email_status=b['status'], email_sources=b['sources'],
                       email_verification=b['verification'],
                       email_confidence={'VERIFIED': 95, 'SOURCE_CONFIRMED': 80, 'LIKELY': 60}[b['status']])
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
        if raw:
            return self.enr._extract_domain(raw)
        if spoc.get('company'):
            return self.enr._find_company_domain({'company': spoc['company']})
        return None
