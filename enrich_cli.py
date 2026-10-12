#!/usr/bin/env python3
"""
SPOC contact completion CLI for agent pipelines (JSON in, JSON out, resumable).

    python enrich_cli.py spocs.json out.json [--batch 10] [--workers 3] [--no-smtp] [--erp rows.json [--erp-min-status VERIFIED]]

Input : [{"lead_id": "1", "name": "Jane Doe", "role": "CEO", "company": "Acme", "domain": "acme.com",
          "email": "", "phone": ""}]        (name and domain (or website) are required;
                                              email/phone are kept and verified if the agent already has them)
Output: the same SPOC with email (+ status VERIFIED | SOURCE_CONFIRMED | LIKELY | UNVERIFIED | NOT_FOUND,
        confidence, sources, verification detail), phone (+ scope person|company), other candidates,
        `gaps` listing everything still missing, and provenance.

ERP : --erp rows.json also writes `leadContacts` rows (see app/enrichment/erp.py). Optional input fields it uses:
      contact_id, din, linkedin_url, data_provenance, consent_basis, is_primary. `lead_id` must be the leadMaster
      record id (it becomes the relation column leadId). workEmail is written only at --erp-min-status or better
      (default VERIFIED); phoneNumber only if it is the person's own number; empty values are omitted, not nulled.

Sources: company site (+discovered team pages, sitemap, schema.org), web search (SERPER_API_KEY / TAVILY_API_KEY),
Hunter (HUNTER_API_KEY), pattern + SMTP; verifiers: SMTP then ZeroBounce (ZEROBOUNCE_API_KEY). No social-profile
scraping. Each run handles at most --batch pending SPOCs then exits, so it fits short agent sessions;
re-run until "remaining" is 0.
"""

import argparse
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.enrichment.erp import STATUS_ORDER, to_lead_contact
from app.enrichment.spoc import SpocFinder


def _key(spoc: dict) -> str:
    return str(spoc.get('contact_id') or spoc.get('lead_id'))


def enrich_one(spoc: dict, finder: SpocFinder) -> dict:
    return finder.find(spoc)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('input')
    ap.add_argument('output')
    ap.add_argument('--batch', type=int, default=10, help='max SPOCs per run (default 10)')
    ap.add_argument('--workers', type=int, default=3, help='SPOCs processed in parallel (default 3)')
    ap.add_argument('--no-smtp', action='store_true', help='skip MX/SMTP checks (use when port 25 is blocked)')
    ap.add_argument('--erp', default=None, help='also write ERP leadContacts rows (camelCase fields) to this file')
    ap.add_argument('--erp-min-status', default='VERIFIED', choices=STATUS_ORDER[1:],
                    help='lowest email_status written to workEmail in --erp rows (default VERIFIED)')
    a = ap.parse_args(argv)

    spocs = json.loads(Path(a.input).read_text())
    out_path, state_path = Path(a.output), Path(a.output + '.state.json')
    results = {r.get('key') or str(r['lead_id']): r for r in json.loads(out_path.read_text())} if out_path.exists() else {}
    done = set(json.loads(state_path.read_text())) if state_path.exists() else set()

    by_key = {_key(s): s for s in spocs}
    pending = [s for s in spocs if _key(s) not in done]
    finder = SpocFinder(smtp=not a.no_smtp)
    lock = threading.Lock()

    def work(spoc):
        key = _key(spoc)
        try:
            res = enrich_one(spoc, finder)
        except Exception as e:                        # one bad record must not lose the batch
            res = {'lead_id': spoc.get('lead_id'), 'email': '', 'email_status': 'ERROR', 'gaps': [f'error:{type(e).__name__}']}
        res['key'] = key
        with lock:
            results[key] = res
            done.add(key)
            out_path.write_text(json.dumps(list(results.values()), indent=2))   # persist after every SPOC
            state_path.write_text(json.dumps(sorted(done)))
            if a.erp:
                Path(a.erp).write_text(json.dumps([to_lead_contact(by_key[k], r, a.erp_min_status) for k, r in results.items() if k in by_key], indent=2))

    with ThreadPoolExecutor(max_workers=max(a.workers, 1)) as ex:
        list(ex.map(work, pending[:a.batch]))

    print(json.dumps({'processed': min(len(pending), a.batch), 'remaining': max(len(pending) - a.batch, 0), 'output': str(out_path)}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
