"""
Shared Scraper Utilities

Common functions used across all platform scrapers.

Provides:
- extract_email() — regex-based email extraction from text
- extract_phone() — phone number extraction from text
- parse_abbreviated_number() — converts "11.5K", "2.3M" to integers
"""

import re


EMAIL_RE = r'\b[A-Za-z0-9._%+-]+@(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}\b'


def extract_email(text: str) -> str:
    """Extract first email address from text."""
    if not text:
        return ''
    matches = re.findall(EMAIL_RE, text)
    return matches[0] if matches else ''


def extract_phone(text: str) -> str:
    """Extract first phone number (10+ digits) from text."""
    if not text:
        return ''
    patterns = [
        r'\+\d{1,3}[-.\s]?\(?\d{2,5}\)?(?:[-.\s]?\d{2,5}){2,3}',   # international, needs a leading +
        r'\(\d{3}\)[-.\s]?\d{3}[-.\s]?\d{4}',
        r'\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b',
        r'\b[6-9]\d{4}[-.\s]?\d{5}\b',                              # Indian mobile
    ]
    for pattern in patterns:
        for m in re.findall(pattern, text):
            phone = re.sub(r'[^\d+]', '', m)
            if 10 <= len(re.sub(r'\D', '', phone)) <= 15:
                return phone
    return ''


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
