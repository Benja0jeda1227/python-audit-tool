"""Integrate existing discovery CSVs offline; optionally run the v2.1 web audit."""
import argparse
import json
from pathlib import Path

from candidate_integration import integrate


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path)
    parser.add_argument('--review', type=Path, default=Path('input/candidate_review.csv'))
    parser.add_argument('--out', type=Path)
    parser.add_argument('--demo', action='store_true')
    parser.add_argument('--audit', action='store_true', help='Explicitly enable live HTTP website auditing')
    parser.add_argument('--identity-map', type=Path, help='Explicit JSON mapping of reviewed aliases to canonical IDs')
    args = parser.parse_args(argv)
    if args.demo and args.audit:
        parser.error('--demo cannot enable live auditing')
    source = args.input or Path('output/candidates_DEMO.csv' if args.demo else 'output/candidates.csv')
    out = args.out or Path('output/integration_v2_2_DEMO' if args.demo else 'output/integration_v2_2')
    try:
        identity_map = None
        if args.identity_map:
            if args.identity_map.name.casefold().startswith('.env') or args.identity_map.resolve().name.casefold().startswith('.env'):
                raise ValueError('Credential files cannot be identity maps')
            identity_map = json.loads(args.identity_map.read_text(encoding='utf-8-sig'))
            if not isinstance(identity_map, dict): raise ValueError('Identity map must be a JSON object')
        target, report = integrate(source, args.review, out, demo=args.demo, identity_map=identity_map)
    except (ValueError, OSError, UnicodeError) as exc:
        parser.error(str(exc))
    print(f'Integrated CSV: {target}; {report["counts"]}')
    if report['conflicts']:
        print('Unresolved manual conflicts: consult integration_report.json. Audit postponed.')
        return 2
    if args.audit:
        import website_audit_v2_1
        snapshot = out / '.integration' / report['generation'] / 'candidate_review.csv'
        return website_audit_v2_1.main(['--input', str(snapshot), '--out', str(out / 'audit')])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
