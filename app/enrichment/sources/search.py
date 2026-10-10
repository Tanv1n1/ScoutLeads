"""Web search. Backends are tried in order until one returns results; results are cached.

Backends are tried in order:
  SERPER_API_KEY   serper.dev   (2,500 free queries at signup, no card)
  TAVILY_API_KEY   tavily.com   (1,000 free credits a month, no card)
"""

import html as htmllib
import logging
import os
from typing import Dict, List

import httpx

from app.common import cache

logger = logging.getLogger(__name__)

KEYS = (('serper', 'SERPER_API_KEY'), ('tavily', 'TAVILY_API_KEY'))


def backends() -> List[str]:
    return [name for name, key in KEYS if os.environ.get(key)]


def available() -> bool:
    return bool(backends())


def _serper(q: str, limit: int) -> List[Dict]:
    r = httpx.post('https://google.serper.dev/search', json={'q': q, 'num': limit},
                   headers={'X-API-KEY': os.environ['SERPER_API_KEY']}, timeout=20)
    r.raise_for_status()
    return [{'url': x['link'], 'text': f"{x.get('title', '')} {x.get('snippet', '')}"} for x in r.json().get('organic', [])]


def _tavily(q: str, limit: int) -> List[Dict]:
    r = httpx.post('https://api.tavily.com/search', json={'query': q, 'max_results': min(limit, 20)},
                   headers={'Authorization': f"Bearer {os.environ['TAVILY_API_KEY']}"}, timeout=30)
    r.raise_for_status()
    return [{'url': x['url'], 'text': f"{x.get('title', '')} {x.get('content', '')}"} for x in r.json().get('results', [])]


def search(query: str, limit: int = 8) -> List[Dict]:
    cached = cache.get('search:' + query)
    if cached is not None:
        return cached
    fns = {'serper': _serper, 'tavily': _tavily}
    for name in backends():
        try:
            res = fns[name](query, limit)
        except (httpx.HTTPError, ValueError, KeyError) as e:
            logger.warning(f'{name} search failed: {e}')
            continue
        if res:
            res = [{'url': r['url'], 'text': htmllib.unescape(r['text'])} for r in res]
            cache.put('search:' + query, res)
            return res
    return []
