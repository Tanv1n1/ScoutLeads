"""Shared patterns and extractors used by scouting and enrichment."""

import re
from typing import List

EMAIL_RE = r'\b[A-Za-z0-9._%+-]+@(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}\b'
PHONE_RE = re.compile(r'(?:\+\d{1,3}[\s.-]?)?(?:\(?\d{2,5}\)?[\s.-]?){2,4}\d{2,5}')
EMAIL_BLACKLIST = ('example.com', 'test.com', 'email.com', 'youremail.com', 'sentry.io', 'wixpress.com',
                   'googleapis.com', 'w3.org', 'schema.org', 'gravatar.com', 'wordpress.com')
FILE_EXT_BLACKLIST = ('.png', '.jpg', '.gif', '.css', '.js', '.svg', '.webp', '.ico')
_PLACEHOLDER_LOCAL = re.compile(r'^(?:[a-f0-9]{16,}|(?:you|your|name|user|email|john\.?doe|jane\.?doe|jane\.diaz)\b)')


def valid_email(email: str) -> bool:
    """False for example/placeholder addresses, file names that look like emails, and tracking domains."""
    lower = email.lower()
    local, _, domain = lower.rpartition('@')
    if any(domain == b or domain.endswith('.' + b) for b in EMAIL_BLACKLIST):
        return False
    return not (lower.endswith(FILE_EXT_BLACKLIST) or _PLACEHOLDER_LOCAL.match(local))


def emails_in(text: str) -> List[str]:
    """Valid email addresses in text, in order of appearance, without duplicates."""
    return list(dict.fromkeys(e for e in re.findall(EMAIL_RE, text or '') if valid_email(e)))


def extract_email(text: str) -> str:
    """First valid email address in text, or ''."""
    found = emails_in(text)
    return found[0] if found else ''


def phones_in(text: str) -> List[str]:
    """Phone numbers (10-13 digits, + kept) in text, in order of appearance, without duplicates."""
    out = []
    for m in PHONE_RE.findall(text or ''):
        digits = re.sub(r'\D', '', m)
        bare = m.strip().isdigit()       # no + or separators: only plausible as an Indian mobile or a 0-prefixed landline
        if bare and not (re.fullmatch(r'[6-9]\d{9}', digits) or re.fullmatch(r'0\d{10}', digits)):
            continue
        if 10 <= len(digits) <= 13 and not re.fullmatch(r'(\d)\1+', digits) and not re.match(r'(19|20)\d{2}', digits):
            norm = ('+' if m.strip().startswith('+') else '') + digits
            if norm not in out:
                out.append(norm)
    return out


def parse_abbreviated_number(s: str) -> int:
    """Parse abbreviated numbers like 11M, 7.5K, 1.2B into integers."""
    s = s.strip().replace(',', '')
    multipliers = {'K': 1_000, 'M': 1_000_000, 'B': 1_000_000_000}

    for suffix, mult in multipliers.items():
        if s.upper().endswith(suffix):
            try:
                return int(float(s[:-1]) * mult)
            except (ValueError, IndexError):
                return 0

    try:
        return int(float(s))
    except ValueError:
        return 0
