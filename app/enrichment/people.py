"""Name, phone and page-text helpers for matching public contact data to a specific person."""

import html as htmllib
import json
import re
from typing import Dict

from app.enrichment.lead import ROLE_LOCALS

TITLES = ('mr', 'ms', 'mrs', 'dr', 'shri', 'smt', 'ca', 'cs')


def page_text(page: str) -> str:
    page = re.sub(r'<(script|style)[^>]*>.*?</\1>', ' ', page, flags=re.S | re.I)
    page = re.sub(r'<a [^>]*href=["\'](?:mailto|tel):([^"\'?]+)[^>]*>', r' \1 ', page, flags=re.I)  # keep mailto/tel targets
    page = re.sub(r'<[^>]+>', ' ', page)
    return re.sub(r'\s+', ' ', htmllib.unescape(page))


def name_parts(full_name: str):
    parts = [p for p in re.sub(r'[^A-Za-z\s.-]', ' ', full_name or '').lower().replace('.', ' ').split() if len(p) > 1 or p.isalpha()]
    parts = [p for p in parts if p not in TITLES]
    if len(parts) < 2:
        return (parts[0] if parts else ''), ''
    return parts[0], parts[-1]


def local_matches_person(local: str, first: str, last: str) -> bool:
    """Does the local part look like this person's name (not a role inbox, not someone else)?"""
    local = re.sub(r'[^a-z]', '', local.lower().split('+')[0])
    if not first or local in ROLE_LOCALS or not local:
        return False
    if not last:
        return local == first
    forms = {first, first + last, last + first, first[0] + last, first + last[0], last, last + first[0]}
    return local in forms



def json_ld(html: str) -> Dict:
    """schema.org data: {'people': [{name,email,phone,title}], 'org_emails': [], 'org_phones': []}."""
    people, org_emails, org_phones = [], [], []

    def clean_mail(v):
        return re.sub(r'^mailto:', '', str(v or ''), flags=re.I).strip().lower()

    def walk(o):
        if isinstance(o, list):
            for x in o:
                walk(x)
        elif isinstance(o, dict):
            t = o.get('@type')
            types = t if isinstance(t, list) else [t]
            if 'Person' in types and o.get('name'):
                people.append({'name': str(o['name']), 'email': clean_mail(o.get('email')),
                               'phone': str(o.get('telephone') or ''), 'title': str(o.get('jobTitle') or '')})
            elif any(x in types for x in ('Organization', 'LocalBusiness', 'Corporation', 'ContactPoint')):
                if o.get('email'):
                    org_emails.append(clean_mail(o['email']))
                if o.get('telephone'):
                    org_phones.append(str(o['telephone']))
            for v in o.values():
                if isinstance(v, (list, dict)):
                    walk(v)

    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html or '', flags=re.S | re.I):
        try:
            walk(json.loads(htmllib.unescape(block).strip()))
        except ValueError:
            continue
    return {'people': people, 'org_emails': org_emails, 'org_phones': org_phones}
