#!/usr/bin/env python3
"""
SPOC contact completion CLI for agent pipelines (JSON in, JSON out, resumable).

    python enrich_cli.py spocs.json out.json [--batch 10] [--state state.json] [--no-smtp]

Input : [{"lead_id": "1", "name": "Jane Doe", "role": "CEO", "company": "Acme", "domain": "acme.com",
          "email": "", "phone": ""}]        (only name + one of company/domain is required;
                                              email/phone are kept and verified if the agent already has them)
Output: the same SPOC with email (+ status VERIFIED | SOURCE_CONFIRMED | LIKELY | UNVERIFIED | NOT_FOUND,
        confidence, sources, verification detail), phone (+ scope person|company), other candidates,
        `gaps` listing everything still missing, and provenance.

Sources: company site, web search (BRAVE_API_KEY), Hunter (HUNTER_API_KEY), pattern + SMTP. No social-profile
scraping. Each run handles at most --batch pending SPOCs then exits, so it fits short agent sessions;
re-run until "remaining" is 0.
"""

import argparse
import json
import sys
from pathlib import Path

from app.spoc import SpocFinder


def enrich_one(spoc: dict, finder: SpocFinder) -> dict:
    return finder.find(spoc)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('input')
    ap.add_argument('output')
    ap.add_argument('--batch', type=int, default=10, help='max SPOCs per run (default 10)')
    ap.add_argument('--state', default=None, help='state file (default: <output>.state.json)')
    ap.add_argument('--no-smtp', action='store_true', help='skip MX/SMTP checks (use when port 25 is blocked)')
    a = ap.parse_args(argv)

    spocs = json.loads(Path(a.input).read_text())
    out_path, state_path = Path(a.output), Path(a.state or a.output + '.state.json')
    results = {str(r['lead_id']): r for r in json.loads(out_path.read_text())} if out_path.exists() else {}
    done = set(json.loads(state_path.read_text())) if state_path.exists() else set()

    pending = [s for s in spocs if str(s.get('lead_id')) not in done]
    finder = SpocFinder(smtp=not a.no_smtp)
    for spoc in pending[:a.batch]:
        key = str(spoc.get('lead_id'))
        results[key] = enrich_one(spoc, finder)
        done.add(key)
        out_path.write_text(json.dumps(list(results.values()), indent=2))   # persist after every SPOC
        state_path.write_text(json.dumps(sorted(done)))

    print(json.dumps({'processed': min(len(pending), a.batch), 'remaining': max(len(pending) - a.batch, 0), 'output': str(out_path)}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
