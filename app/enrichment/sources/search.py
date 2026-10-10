"""Web search backends. Tried in order until one returns results; results are cached."""

import html as htmllib
import logging
import os
from typing import Dict, List

import httpx

from app.common import cache

logger = logging.getLogger(__name__)


def backends() -> List[str]:
    return [n for n, k in (('brave', 'BRAVE_API_KEY'), ('serper', 'SERPER_API_KEY')) if os.environ.get(k)]


def _brave(q: str, limit: int) -> List[Dict]:
    r = httpx.get('https://api.search.brave.com/res/v1/web/search', params={'q': q, 'count': limit},
                  headers={'X-Subscription-Token': os.environ['BRAVE_API_KEY'], 'Accept': 'application/json'}, timeout=15)
    r.raise_for_status()
    return [{'url': x['url'], 'text': f"{x.get('title', '')} {x.get('description', '')}"}
            for x in r.json().get('web', {}).get('results', [])]


def _serper(q: str, limit: int) -> List[Dict]:
    r = httpx.post('https://google.serper.dev/search', json={'q': q, 'num': limit},
                   headers={'X-API-KEY': os.environ['SERPER_API_KEY']}, timeout=15)
    r.raise_for_status()
    return [{'url': x['link'], 'text': f"{x.get('title', '')} {x.get('snippet', '')}"} for x in r.json().get('organic', [])]


def search(query: str, limit: int = 8) -> List[Dict]:
    cached = cache.get('search:' + query)
    if cached is not None:
        return cached
    for name in backends():
        try:
            res = {'brave': _brave, 'serper': _serper}[name](query, limit)
        except (httpx.HTTPError, ValueError, KeyError) as e:
            logger.warning(f'{name} search failed: {e}')
            continue
        if res:
            res = [{'url': r['url'], 'text': htmllib.unescape(r['text'])} for r in res]
            cache.put('search:' + query, res)
            return res
    return []
