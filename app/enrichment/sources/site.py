"""The company's own site: fixed team/contact paths + links found on the homepage + sitemap."""

import re
from typing import Callable, Dict, List, Optional
from urllib.parse import urljoin, urlparse

from app.common.net import random_delay

STATIC_PATHS = ('', '/contact', '/contact-us', '/about', '/about-us', '/team', '/our-team',
                '/leadership', '/management', '/people', '/company', '/founders')
WANTED = re.compile(r'team|leader|manage|people|founder|director|about|contact|story|who-we-are|board|meet|c-suite|executive', re.I)


def _same_site_path(url: str, domain: str) -> Optional[str]:
    p = urlparse(url)
    host = p.netloc.lower().replace('www.', '')
    if host and host != domain:
        return None
    if re.search(r'\.(?:pdf|jpg|jpeg|png|gif|zip|css|js)$', p.path, re.I):
        return None
    return p.path.rstrip('/') or '/'


def discover_links(html: str, base: str, domain: str, limit: int = 8) -> List[str]:
    out = []
    for href in re.findall(r'<a\s[^>]*href=["\']([^"\'#]+)', html or '', flags=re.I):
        path = _same_site_path(urljoin(base, href), domain)
        if path and path != '/' and WANTED.search(path) and path not in out:
            out.append(path)
        if len(out) >= limit:
            break
    return out


def sitemap_paths(fetch: Callable[[str], Optional[str]], domain: str, limit: int = 8) -> List[str]:
    xml = fetch(f'https://{domain}/sitemap.xml') or ''
    locs = re.findall(r'<loc>\s*([^<\s]+)\s*</loc>', xml)
    nested = [l for l in locs if l.endswith('.xml')][:2]      # sitemap index: look one level down
    for n in nested:
        locs += re.findall(r'<loc>\s*([^<\s]+)\s*</loc>', fetch(n) or '')
    out = []
    for l in locs:
        path = _same_site_path(l, domain)
        if path and path != '/' and WANTED.search(path) and path not in out and not path.endswith('.xml'):
            out.append(path)
        if len(out) >= limit:
            break
    return out


def crawl(fetch: Callable[[str], Optional[str]], domain: str, max_pages: int = 18) -> Dict[str, str]:
    pages: Dict[str, str] = {}

    def grab(path: str):
        if path in pages or len(pages) >= max_pages:
            return
        html = fetch(f'https://{domain}{"" if path == "/" else path}')
        if html:
            pages[path or '/'] = html
        random_delay(0.15, 0.4)

    for p in STATIC_PATHS:
        grab(p or '/')
    home = pages.get('/', '')
    for p in discover_links(home, f'https://{domain}/', domain):
        grab(p)
    for p in sitemap_paths(fetch, domain):
        grab(p)
    return pages
