"""Web search via the Brave Search API (BRAVE_API_KEY). Results are cached."""

import html as htmllib
import logging
import os
from typing import Dict, List

import httpx

from app.common import cache

logger = logging.getLogger(__name__)


def available() -> bool:
    return bool(os.environ.get('BRAVE_API_KEY'))


def search(query: str, limit: int = 8) -> List[Dict]:
    if not available():
        return []
    cached = cache.get('search:' + query)
    if cached is not None:
        return cached
    try:
        r = httpx.get('https://api.search.brave.com/res/v1/web/search', params={'q': query, 'count': limit},
                      headers={'X-Subscription-Token': os.environ['BRAVE_API_KEY'], 'Accept': 'application/json'}, timeout=15)
        r.raise_for_status()
        res = [{'url': x['url'], 'text': htmllib.unescape(f"{x.get('title', '')} {x.get('description', '')}")}
               for x in r.json().get('web', {}).get('results', [])]
    except (httpx.HTTPError, ValueError, KeyError) as e:
        logger.warning(f'search failed: {e}')
        return []
    cache.put('search:' + query, res)
    return res
