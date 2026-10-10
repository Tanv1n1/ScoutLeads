"""
Business discovery for cross-border / recurring-payment prospects.

search -> analyze site for signals -> fit score -> contact emails.
"""

import html as htmllib
import re
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional
from urllib.parse import urlparse, parse_qs, unquote

import httpx

from app.common.net import random_user_agent, random_delay, get_httpx_proxy
from app.enrichment.lead import LeadEnricher
from app.enrichment.sources import search as web
from app.common.text import emails_in, phones_in

logger = logging.getLogger(__name__)

QUERY_TEMPLATES = {
    'exporters': [
        '{geo} exporter manufacturer "worldwide shipping" contact',
        '{geo} export company "we export to" countries contact us',
        '{geo} B2B wholesale supplier international buyers inquiry',
    ],
    'ecommerce': [
        '{geo} brand "international shipping" multi-currency store',
        '{geo} D2C store "ships worldwide" USD GBP EUR',
    ],
    'saas': [
        '{geo} SaaS pricing "per month" "billed annually" USD',
        '{geo} software subscription pricing plans global customers',
    ],
    'agencies': [
        '{geo} agency "monthly retainer" international clients USD',
        '{geo} freelance development studio overseas clients contact',
    ],
}

# Hosts that are never the prospect itself.
SKIP_HOSTS = (
    'google.', 'bing.', 'duckduckgo.', 'facebook.', 'instagram.', 'linkedin.', 'twitter.',
    'x.com', 'youtube.', 'wikipedia.', 'quora.', 'reddit.', 'pinterest.', 'amazon.', 'alibaba.',
    'indiamart.', 'tradeindia.', 'exportersindia.', 'justdial.', 'crunchbase.', 'glassdoor.',
    'medium.com', 'clutch.co', 'g2.com', 'capterra.', 'trustpilot.', 'yelp.', 'ebay.',
)

# (signal name, points, regex over lowercased page text/HTML)
SIGNALS = [
    # cross-border
    ('ships_worldwide', 15, r'ship(?:s|ping)? (?:worldwide|internationally|globally)|worldwide (?:shipping|delivery)|international (?:shipping|orders|clients|customers|buyers)'),
    ('export_language', 15, r'\bexport(?:er|ers|ing)?\b|\bimport(?:er|ers|ing)?\b|incoterms|\bfob\b|\bcif\b|letter of credit'),
    ('multi_currency', 15, r'\b(?:usd|eur|gbp|aed|sgd|aud|cad)\b|currency (?:switcher|selector)|select(?:ed)? currency'),
    ('countries_served', 10, r'(?:customers|clients|buyers|serving|exports?) (?:in|to|across) \d+\+? countries|\d+\+? countries'),
    # recurring
    ('subscription', 15, r'per month|/\s?mo\b|monthly plan|billed (?:monthly|annually|yearly)|subscription|monthly retainer|recurring'),
    ('billing_provider', 20, r'stripe|paddle|chargebee|recurly|razorpay subscriptions|paypal subscriptions|checkout\.stripe|fastspring|2checkout|wise business|payoneer|airwallex'),
]

ROLE_PREFIXES = ('export', 'sales', 'finance', 'accounts', 'billing', 'ceo', 'founder', 'director', 'contact', 'info', 'hello')


def search(query: str, limit: int = 10) -> List[str]:
    """Shared API search if any key is set (SERPER / TAVILY / BRAVE), else best-effort DuckDuckGo (often bot-challenged)."""
    if web.available():
        return [r['url'] for r in web.search(query, limit)]
    try:
        resp = httpx.post(
            'https://html.duckduckgo.com/html/',
            data={'q': query},
            headers={'User-Agent': random_user_agent()},
            timeout=15, follow_redirects=True, proxy=get_httpx_proxy(),
        )
    except httpx.RequestError as e:
        logger.debug(f"search failed: {e}")
        return []
    if resp.status_code != 200:
        logger.debug(f"search HTTP {resp.status_code}")
        return []

    urls = []
    for href in re.findall(r'class="result__a"[^>]*href="([^"]+)"', resp.text):
        if 'uddg=' in href:
            href = unquote(parse_qs(urlparse(href).query).get('uddg', [''])[0])
        if href.startswith('http'):
            urls.append(href)
        if len(urls) >= limit:
            break
    return urls


def _root(url: str) -> Optional[str]:
    p = urlparse(url)
    host = p.netloc.lower().replace('www.', '')
    if not host or any(s in host for s in SKIP_HOSTS):
        return None
    return f'{p.scheme}://{p.netloc}'


def analyze_site(root: str, enricher: LeadEnricher) -> Optional[Dict]:
    """Fetch key pages, detect signals, collect emails. None if site unreachable."""
    pages = {}
    for path in ('', '/pricing', '/about', '/contact', '/shipping', '/export'):
        html = enricher._fetch_page(root + path)
        if html:
            pages[path] = html
        random_delay(0.2, 0.5)
    if not pages:
        return None

    blob = ' '.join(pages.values()).lower()
    hits, score = [], 0
    for name, points, pattern in SIGNALS:
        if re.search(pattern, blob):
            hits.append(name)
            score += points

    emails, phone = set(), None
    for html in pages.values():
        html = re.sub(r'\\u00[0-9a-fA-F]{2}', ' ', htmllib.unescape(html))
        emails.update(emails_in(html))
        phone = phone or next(iter(phones_in(html)), None)

    domain = urlparse(root).netloc.lower().replace('www.', '')
    own = [e for e in emails if e.lower().endswith('@' + domain)]
    own.sort(key=lambda e: next((i for i, r in enumerate(ROLE_PREFIXES) if e.lower().startswith(r)), 99))

    title = re.search(r'<title[^>]*>(.*?)</title>', pages.get('', ''), re.I | re.S)
    cross, recurring = {'ships_worldwide', 'export_language', 'multi_currency', 'countries_served'}, {'subscription', 'billing_provider'}
    return {
        'platform': 'discovery',
        'company': re.sub(r'\s+', ' ', title.group(1)).strip()[:80] if title else domain,
        'website': root,
        'domain': domain,
        'signals': ','.join(hits),
        'cross_border': bool(cross & set(hits)),
        'recurring': bool(recurring & set(hits)),
        'fit_score': min(score, 100),
        'email': own[0] if own else '',
        'other_emails': ';'.join(own[1:4]),
        'own_emails': own,
        'phone': phone or '',
    }


def discover(categories: List[str], geo: str = 'India', per_query: int = 10,
             min_score: int = 30, on_progress=None, seed_urls: Optional[List[str]] = None) -> List[Dict]:
    """Search (or take seed_urls), analyze each unique site, return leads ranked by fit score."""
    roots = {}
    for u in seed_urls or []:
        r = _root(u if u.startswith('http') else 'https://' + u)
        if r:
            roots[r] = 'seed'
    for cat in ([] if seed_urls else categories):
        for tpl in QUERY_TEMPLATES.get(cat, []):
            for url in search(tpl.format(geo=geo), per_query):
                r = _root(url)
                if r and r not in roots:
                    roots[r] = cat
            random_delay(1.5, 3.0)

    enricher = LeadEnricher()
    leads = []

    def work(item):
        root, cat = item
        lead = analyze_site(root, enricher)
        if lead:
            lead['category'] = cat
        if on_progress:
            on_progress(root)
        return lead

    with ThreadPoolExecutor(max_workers=4) as ex:
        for lead in ex.map(work, roots.items()):
            if lead and lead['fit_score'] >= min_score and (lead['cross_border'] or lead['recurring']):
                leads.append(lead)

    leads.sort(key=lambda l: l['fit_score'], reverse=True)
    return leads
