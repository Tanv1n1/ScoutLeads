"""Email verification: MX + SMTP (with catch-all probe), plus Hunter / ZeroBounce APIs when SMTP can't decide."""

import os
from typing import Dict

import httpx

from app.common import cache

# ZeroBounce status -> our vocabulary
_ZB = {'valid': 'valid', 'invalid': 'invalid', 'spamtrap': 'invalid', 'abuse': 'invalid',
       'do_not_mail': 'invalid', 'catch-all': 'accept_all'}


class Verifier:

    def __init__(self, enricher, smtp: bool = True):
        self.enr = enricher
        self.smtp = smtp

    def verify(self, email: str) -> Dict:
        cached = cache.get('verify:' + email)
        if cached:
            return cached
        v = {'mx': False, 'smtp': 'skipped', 'catch_all': None, 'api': {}}
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

        if v['smtp'] in ('unknown', 'skipped') or v['catch_all']:     # SMTP cannot decide: ask an API
            if os.environ.get('HUNTER_API_KEY'):
                v['api']['hunter'] = self._hunter(email)
            if os.environ.get('ZEROBOUNCE_API_KEY'):
                v['api']['zerobounce'] = self._zerobounce(email)
            v['api'] = {k: s for k, s in v['api'].items() if s}

        if v['smtp'] != 'unknown' or v['api']:        # don't cache a transient network failure
            cache.put('verify:' + email, v)
        return v

    @staticmethod
    def _hunter(email: str):
        try:
            d = httpx.get('https://api.hunter.io/v2/email-verifier',
                          params={'email': email, 'api_key': os.environ['HUNTER_API_KEY']}, timeout=25).json().get('data', {})
            return d.get('status')                      # valid | invalid | accept_all | webmail | unknown
        except (httpx.HTTPError, ValueError):
            return None

    @staticmethod
    def _zerobounce(email: str):
        try:
            d = httpx.get('https://api.zerobounce.net/v2/validate',
                          params={'api_key': os.environ['ZEROBOUNCE_API_KEY'], 'email': email}, timeout=25).json()
            return _ZB.get(str(d.get('status', '')).lower(), 'unknown')
        except (httpx.HTTPError, ValueError):
            return None


def judge(v: Dict) -> str:
    """'invalid' | 'valid' | 'undecided' from one verification result."""
    api = set((v.get('api') or {}).values())
    if v['smtp'] in ('rejected', 'no_mx') or 'invalid' in api:
        return 'invalid'
    if (v['smtp'] == 'accepted' and not v['catch_all']) or 'valid' in api:
        return 'valid'
    return 'undecided'
