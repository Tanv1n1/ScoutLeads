"""Map a SpocFinder result to an ERP `leadContacts` row (schema: lead_contacts).

Row keys are the ERP field names (camelCase). Empty values are omitted, never sent as null, so an upsert on
`contactId` can't blank out data already in the ERP. Nothing is invented: din / linkedinUrl / provenance /
consent come from the input record when the caller has them.
"""

import re
from typing import Dict

STATUS_ORDER = ['UNVERIFIED', 'LIKELY', 'SOURCE_CONFIRMED', 'VERIFIED']
DEFAULT_PROVENANCE = 'public_web_sources'      # used when the caller doesn't say where the SPOC came from
DEFAULT_CONSENT = 'legitimate-interest-b2b'    # documented value for the consentBasis field


def contact_id(lead_id, name: str) -> str:
    """Stable upsert key: the same lead + person always maps to the same id."""
    slug = re.sub(r'[^a-z0-9]+', '-', (name or '').lower()).strip('-')
    return f'{lead_id}__{slug}' if slug else str(lead_id)


def to_lead_contact(spoc: Dict, result: Dict, min_status: str = 'VERIFIED') -> Dict:
    """Build one leadContacts row.

    workEmail is written only when the result's email_status is at least `min_status` (default VERIFIED);
    lower-confidence addresses stay in the full result. phoneNumber is written only when it is the person's
    own number (or one the caller provided), never a company switchboard.
    """
    status = result.get('email_status', '')
    email_ok = bool(result.get('email')) and status in STATUS_ORDER and STATUS_ORDER.index(status) >= STATUS_ORDER.index(min_status)
    phone_ok = result.get('phone_scope') in ('person', 'provided')
    name = spoc.get('name') or spoc.get('full_name') or ''
    row = {
        'contactId': spoc.get('contact_id') or contact_id(spoc.get('lead_id'), name),
        'name': name,
        'fullName': name,
        'designation': spoc.get('role') or spoc.get('designation'),
        'din': spoc.get('din'),
        'linkedinUrl': spoc.get('linkedin_url'),
        'workEmail': result['email'] if email_ok else None,
        'phoneNumber': result['phone'] if phone_ok and result.get('phone') else None,
        'dataProvenance': spoc.get('data_provenance') or DEFAULT_PROVENANCE,
        'consentBasis': spoc.get('consent_basis') or DEFAULT_CONSENT,
        'isPrimary': spoc.get('is_primary', True),
        'leadId': spoc.get('lead_id'),
    }
    return {k: v for k, v in row.items() if v is not None and v != ''}
