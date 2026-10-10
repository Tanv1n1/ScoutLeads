import os
import random
import time
from typing import Optional

USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Safari/605.1.15',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
]


def random_user_agent() -> str:
    return random.choice(USER_AGENTS)


def random_delay(min_seconds: float = 1.5, max_seconds: float = 4.5):
    time.sleep(random.uniform(min_seconds, max_seconds))


def get_httpx_proxy() -> Optional[str]:
    """Optional single egress proxy from SCOUT_PROXY."""
    proxy = os.environ.get('SCOUT_PROXY')
    if proxy and not proxy.startswith('http'):
        proxy = f'http://{proxy}'
    return proxy or None
