"""Low-level web and mail helpers used by the SPOC finder: page fetch, SMTP check, email patterns, Hunter."""

import logging
import os
import re
import smtplib
import socket
from typing import Dict, Optional
from urllib.parse import urlparse

import dns.resolver
import httpx

from app.common.net import random_user_agent

logger = logging.getLogger(__name__)

SMTP_HELO = os.environ.get('SCOUT_SMTP_HELO', 'scout-verify.local')
SMTP_FROM = os.environ.get('SCOUT_SMTP_FROM', 'verify@scout-verify.local')
EMAIL_BLACKLIST = ['example.com', 'test.com', 'email.com', 'youremail.com',
                   'sentry.io', 'wixpress.com', 'googleapis.com', 'w3.org',
                   'schema.org', 'gravatar.com', 'wordpress.com']
ROLE_LOCALS = {'info', 'contact', 'hello', 'sales', 'support', 'admin', 'office', 'team', 'enquiry',
               'enquiries', 'inquiry', 'mail', 'accounts', 'billing', 'finance', 'hr', 'careers', 'export', 'marketing'}
FILE_EXT_BLACKLIST = ['.png', '.jpg', '.gif', '.css', '.js', '.svg', '.webp', '.ico']


class LeadEnricher:

    def __init__(self, hunter_api_key: Optional[str] = None):
        self.hunter_api_key = hunter_api_key

    def _is_valid_email(self, email: str) -> bool:
        lower = email.lower()
        domain = lower.rsplit('@', 1)[-1]
        if any(domain == b or domain.endswith('.' + b) for b in EMAIL_BLACKLIST):
            return False
        if any(lower.endswith(ext) for ext in FILE_EXT_BLACKLIST):
            return False
        if re.match(r'^(?:[a-f0-9]{16,}|(?:you|your|name|user|email|john\.?doe|jane\.?doe|jane\.diaz)\b)', lower.split('@')[0]):
            return False   # hashes and docs placeholders
        return True

    def _fetch_page(self, url: str) -> Optional[str]:
        try:
            if not url.startswith('http'):
                url = 'https://' + url
            resp = httpx.get(url, timeout=10, headers={
                'User-Agent': random_user_agent()
            }, follow_redirects=True)
            if resp.status_code == 200:
                return resp.text
        except Exception as e:
            logger.debug(f"Error fetching {url}: {e}")
        return None

    def _detect_pattern(self, local_part: str) -> Optional[str]:
        if local_part in ROLE_LOCALS:
            return None
        if '.' in local_part:
            parts = local_part.split('.')
            if len(parts) == 2:
                if len(parts[0]) == 1:
                    return 'f.last'
                return 'first.last'
        if re.match(r'^[a-z][a-z]+$', local_part):
            return 'first'
        if re.match(r'^[a-z]\.[a-z]+$', local_part):
            return 'f.last'
        return None

    def _verify_email_smtp(self, email: str) -> Dict:
        parts = email.split('@')
        if len(parts) != 2 or not parts[1]:
            return {'exists': False, 'accept_all': False, 'score': 0}
        domain = parts[1]
        result = {'exists': False, 'accept_all': False, 'score': 0, 'mx': False, 'checked': False}

        try:
            mx_records = dns.resolver.resolve(domain, 'MX')
            mx_host = str(sorted(mx_records, key=lambda x: x.preference)[0].exchange).rstrip('.')
        except Exception:
            return result

        result['score'] += 10
        result['mx'] = True

        try:
            with smtplib.SMTP(timeout=10) as smtp:
                smtp.connect(mx_host, 25)
                smtp.helo(SMTP_HELO)
                smtp.mail(SMTP_FROM)
                code, msg = smtp.rcpt(email)
                result['checked'] = True

                if code == 250:
                    result['exists'] = True
                    result['score'] += 80

                fake = f'zzznonexistent999@{domain}'
                code2, _ = smtp.rcpt(fake)
                if code2 == 250:
                    result['accept_all'] = True
                    result['score'] = max(result['score'] - 40, 30)

        except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError,
                socket.timeout, ConnectionRefusedError, OSError) as e:
            logger.debug(f"SMTP verify failed for {email}: {e}")   # blocked port 25 etc: unknown, not invalid

        return result

    def _extract_domain(self, website: str) -> Optional[str]:
        try:
            if not website.startswith('http'):
                website = 'https://' + website
            domain = urlparse(website).netloc or website
            return domain.replace('www.', '')
        except Exception:
            return None

    def _find_with_hunter(self, full_name: Optional[str], website: Optional[str]) -> Optional[str]:
        if not self.hunter_api_key or not full_name or not website:
            return None

        try:
            domain = self._extract_domain(website)
            if not domain:
                return None

            parts = full_name.split()
            if len(parts) < 2:
                return None

            resp = httpx.get('https://api.hunter.io/v2/email-finder', params={
                'domain': domain,
                'first_name': parts[0],
                'last_name': parts[-1],
                'api_key': self.hunter_api_key,
            }, timeout=10)

            data = resp.json()
            if data.get('data', {}).get('email'):
                return data['data']['email']
        except Exception as e:
            logger.debug(f"Hunter.io error: {e}")

        return None

    def _apply_pattern(self, pattern: str, first: str, last: str, domain: str) -> Optional[str]:
        local = {'first.last': f'{first}.{last}', 'first': first, 'f.last': f'{first[0]}.{last}',
                 'flast': f'{first[0]}{last}', 'firstlast': f'{first}{last}'}.get(pattern)
        return f'{local}@{domain}' if local else None
