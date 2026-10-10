"""Email verification: MX + SMTP (free, with catch-all probe), then ZeroBounce for addresses that survive.

ZeroBounce (ZEROBOUNCE_API_KEY) is the primary deliverability check and costs one credit per address,
so it is skipped for anything SMTP already proved undeliverable and every result is cached.
"""

import logging
import os
from typing import Dict, Optional

import httpx

from app.common import cache

logger = logging.getLogger(__name__)

# ZeroBounce status -> our vocabulary
_ZB = {'valid': 'valid', 'invalid': 'invalid', 'spamtrap': 'invalid', 'abuse': 'invalid',
       'do_not_mail': 'invalid', 'catch-all': 'accept_all', 'unknown': 'unknown'}


class Verifier:

    def __init__(self, enricher, smtp: bool = True):
        self.enr = enricher
        self.smtp = smtp

    def verify(self, email: str) -> Dict:
        cached = cache.get('verify:' + email)
        if cached:
            return cached
        v = {'mx': False, 'smtp': 'skipped', 'catch_all': None, 'api': {}, 'api_detail': {}}
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

        zb_failed = False
        if v['smtp'] not in ('rejected', 'no_mx'):      # don't pay to check what SMTP already proved dead
            if os.environ.get('ZEROBOUNCE_API_KEY'):
                zb = self._zerobounce(email)
                if zb:
                    v['api']['zerobounce'] = zb['status']
                    v['api_detail']['zerobounce'] = zb['raw']
                else:
                    zb_failed = True

        if not zb_failed and (v['smtp'] != 'unknown' or v['api']):   # don't cache a transient failure
            cache.put('verify:' + email, v)
        return v

    @staticmethod
    def _zerobounce(email: str) -> Optional[Dict]:
        try:
            d = httpx.get('https://api.zerobounce.net/v2/validate',
                          params={'api_key': os.environ['ZEROBOUNCE_API_KEY'], 'email': email}, timeout=30).json()
        except (httpx.HTTPError, ValueError):
            return None
        status = str(d.get('status', '')).lower()
        if status not in _ZB:                           # bad key, no credits, or an error body
            logger.warning(f"ZeroBounce gave no verdict for {email}: {str(d.get('error', d))[:120]}")
            return None
        return {'status': _ZB[status], 'raw': {'status': status, 'sub_status': d.get('sub_status', '')}}


def judge(v: Dict) -> str:
    """'invalid' | 'valid' | 'undecided' from one verification result."""
    api = set((v.get('api') or {}).values())
    if v['smtp'] in ('rejected', 'no_mx') or 'invalid' in api:
        return 'invalid'
    if (v['smtp'] == 'accepted' and not v['catch_all']) or 'valid' in api:
        return 'valid'
    return 'undecided'
