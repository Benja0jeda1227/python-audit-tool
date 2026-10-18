"""Conservative offline integration with persistent identity (v2.2.1)."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from integration_store import Store, ExportBundle, writer_lock
from url_policy import URL_FIELDS, normalize_url, project_row, public_url, redact

REVIEW_FIELDS = ['Candidate ID', 'Company / Group', 'Target Country', 'HQ / Operating Base',
                 'Business Role', 'Manufacturing Confirmed', 'Group Key', 'Project Eligibility',
                 'Exclusion / Inclusion Reason', 'Official Website', 'Products Evidence URL',
                 'Manufacturing Evidence URL', 'Public Contact Page', 'Contact Type', 'Independent QA',
                 'Research Notes', 'Source Language', 'Reviewed On', 'Original Search URL', 'Product Categories']
DISCOVERY_REQUIRED = {'Candidate ID', 'Country', 'Search Result URL', 'Verification Status'}
REVIEW_REQUIRED = {'Candidate ID', 'Company / Group', 'Target Country', 'Official Website', 'Project Eligibility'}
MAPPING = {'Country': 'Target Country', 'Search Result URL': 'Original Search URL',
           'Company Name (manual)': 'Company / Group', 'Official Website (manual)': 'Official Website',
           'Product Category (manual)': 'Product Categories', 'Public Contact Page (manual)': 'Public Contact Page',
           'Source Language': 'Source Language', 'Reviewed On': 'Reviewed On', 'Research Notes': 'Research Notes'}


def read_csv(path: Path, required: set[str]):
    if path.name.casefold().startswith('.env') or path.resolve().name.casefold().startswith('.env'):
        raise ValueError('Credential files cannot be candidate data')
    try:
        with path.open(encoding='utf-8-sig', newline='') as stream:
            reader = csv.DictReader(stream, strict=True)
            fields = reader.fieldnames or []
            if any(not k or k != k.strip() or re.search(r'[\x00-\x1f\x7f]', k) or k.startswith(('=', '+', '-', '@'))
                   for k in fields) or len(fields) != len(set(fields)) or not required.issubset(fields):
                raise ValueError(f'{path}: duplicate/invalid headers or missing required columns')
            rows = []
            for row in reader:
                if None in row or any(v is None for v in row.values()):
                    raise ValueError(f'{path}:{reader.line_num}: malformed CSV row')
                rows.append(row)
            return fields, rows
    except csv.Error as exc:
        raise ValueError(f'{path}: malformed CSV') from exc


def validate_rows(rows, *, demo):
    ids = []
    for row in rows:
        if not isinstance(row, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in row.items()):
            raise ValueError('Candidate rows must contain string fields')
        cid = row.get('Candidate ID', '')
        if not cid or cid != cid.strip() or redact(cid) != cid or re.search(r'[\x00-\x20\x7f]', cid) or cid.startswith(('=', '+', '-', '@')):
            raise ValueError('Review IDs must be nonempty, trimmed and safe')
        if not demo and (cid.upper().startswith(('DEMO-', 'TEST-')) or
                         any('DEMO - NOT REAL' in v.upper() for v in row.values())):
            raise ValueError('DEMO review rows cannot enter live integration')
        ids.append(cid)
    if len(ids) != len(set(ids)):
        raise ValueError('Review IDs must be unique')


def _text(value):
    return ' '.join(value.casefold().split())


def _learn(identities, row, source=None):
    identity = identities.setdefault(row['Candidate ID'], {'search': [], 'official': [], 'ids': []})
    groups = identity.setdefault('groups', [])
    for group in (row.get('Group Key', ''), (source or {}).get('Group Key', '')):
        if group.strip() and _text(group) not in groups:
            groups.append(_text(group))
    pairs = [('search', row.get('Original Search URL', '')), ('official', row.get('Official Website', ''))]
    if source:
        pairs += [('search', source.get('Search Result URL', '')),
                  ('official', source.get('Official Website (manual)', ''))]
    for role, value in pairs:
        key = normalize_url(value)
        if key and key not in identity[role]:
            identity[role].append(key)


def _matches(source, rows, identities):
    key = normalize_url(source.get('Search Result URL', ''))
    official = normalize_url(source.get('Official Website (manual)', ''))
    strong, possible = [], []
    for row in rows:
        identity = identities[row['Candidate ID']]
        exact = (key and key in identity['search']) or (official and official in identity['official'])
        country_conflict = (source.get('Country', '').strip() and row.get('Target Country', '').strip()
                            and _text(source['Country']) != _text(row['Target Country']))
        name_conflict = (source.get('Company Name (manual)', '').strip() and row.get('Company / Group', '').strip()
                         and _text(source['Company Name (manual)']) != _text(row['Company / Group']))
        site_conflict = official and identity['official'] and official not in identity['official']
        if exact:
            (possible if country_conflict or name_conflict or site_conflict else strong).append(row['Candidate ID'])
        elif ((official or key) and any(urlsplit(v).hostname in {urlsplit(key).hostname, urlsplit(official).hostname}
                                       for v in identity['official'])
              or (source.get('Company Name (manual)', '').strip()
                  and _text(source['Company Name (manual)']) == _text(row.get('Company / Group', ''))
                  and _text(source.get('Country', '')) == _text(row.get('Target Country', '')))
              or (source.get('Group Key', '').strip() and _text(source['Group Key']) in identity.get('groups', []))):
            possible.append(row['Candidate ID'])
    return strong, possible


def _url_issues(row):
    return [{'field': k, 'value': redact(v), 'reason': 'Invalid or sensitive URL; not accessible/verified'}
            for k, v in row.items() if k in URL_FIELDS and v.strip() and not public_url(v)]


def merge_candidates(discoveries, reviews, *, demo=False, identities=None):
    """Discovery IDs are never identities. Existing/manual values are not replaced."""
    validate_rows(reviews, demo=demo)
    rows = [redact(dict(r)) for r in reviews]
    identities = identities if identities is not None else {}
    for row in rows:
        _learn(identities, row)
    events = []
    for line, raw_source in enumerate(discoveries, 2):
        source = redact(dict(raw_source))
        issues = _url_issues(raw_source)
        event = {'line': line, 'source': source, 'candidate_id': '', 'matches': [], 'url_issues': issues}
        key = normalize_url(raw_source.get('Search Result URL', ''))
        is_demo = raw_source.get('Verification Status', '').strip().upper().startswith('DEMO')
        if is_demo != demo or not raw_source.get('Candidate ID', '').strip() or not key:
            events.append({**event, 'action': 'rejected', 'reason': 'Invalid URL/ID or DEMO mode mismatch'})
            continue
        usable = dict(source)
        for issue in issues:
            usable[issue['field']] = ''
        strong, possible = _matches(usable, rows, identities)
        matches = sorted(set(strong + possible))
        if len(strong) == 1 and not possible:
            cid = strong[0]
            existing = next(r for r in rows if r['Candidate ID'] == cid)
            suggestions = {target: {'existing': existing.get(target, ''), 'incoming': usable.get(origin, '')}
                           for origin, target in MAPPING.items() if usable.get(origin, '').strip()
                           and usable[origin] != existing.get(target, '') and target != 'Original Search URL'}
            _learn(identities, existing, usable)
            events.append({**event, 'action': 'existing', 'candidate_id': cid, 'matches': strong,
                           'suggestions': suggestions})
        elif matches:
            events.append({**event, 'action': 'ambiguous', 'matches': matches,
                           'reason': 'Manual identity resolution required'})
        else:
            cid = ('DEMO-N-' if demo else 'N-') + uuid.uuid4().hex
            while cid in identities:
                cid = ('DEMO-N-' if demo else 'N-') + uuid.uuid4().hex
            new = dict.fromkeys(REVIEW_FIELDS, '')
            new.update({target: usable.get(origin, '') for origin, target in MAPPING.items()})
            new.update({'Candidate ID': cid, 'Project Eligibility': 'Needs review'})
            rows.append(new)
            _learn(identities, new, usable)
            events.append({**event, 'action': 'new', 'candidate_id': cid})
    return rows, events


def _reconcile(manual, working, state, identities, identity_map=None):
    """Three-way field merge. Unresolved alternatives survive in report/state."""
    old = {r['Candidate ID']: r for r in state.get('rows', [])}
    master_before = {r['Candidate ID']: r for r in state.get('manual_snapshot', [])}
    left = {r['Candidate ID']: r for r in manual}
    right = {r['Candidate ID']: r for r in working}
    conflicts = []
    if identity_map is not None and not isinstance(identity_map, dict):
        raise ValueError('Identity map must be an object')
    identity_map = identity_map or {}
    if any(not isinstance(k, str) or not isinstance(v, str) for k, v in identity_map.items()):
        raise ValueError('Identity resolution IDs must be strings')
    if len(set(identity_map.values())) != len(identity_map):
        raise ValueError('Resolve one reviewed alias per canonical ID at a time')
    for alias, canonical in identity_map.items():
        if not isinstance(alias, str) or not isinstance(canonical, str) or canonical not in set(old) | set(right):
            raise ValueError('Identity resolution must target an existing canonical ID')
        if alias in old or (alias in right and alias != canonical):
            raise ValueError('Identity resolution cannot merge established identities')
        if canonical in left and alias in left and alias != canonical:
            raise ValueError('Keep one reviewed row per explicit canonical identity resolution')
        if alias not in left and alias not in right and not any(alias in i['ids'] for i in identities.values()):
            raise ValueError('Identity resolution source is not present in manual input')
        _learn(identities, old.get(canonical, right.get(canonical, {})))
        identities[canonical]['ids'] = sorted(set(identities[canonical]['ids'] + [alias]))
    # A renumbered working copy is also a reviewed alias, never a second entity.
    for cid in list(right):
        if cid in old or not old:
            continue
        row = right[cid]
        aliases = [canonical for canonical, identity in identities.items() if cid in identity['ids'] and canonical in old]
        incoming = {'Search Result URL': row.get('Original Search URL', ''),
                    'Official Website (manual)': row.get('Official Website', ''),
                    'Company Name (manual)': row.get('Company / Group', ''), 'Country': row.get('Target Country', '')}
        strong, possible = _matches(incoming, list(old.values()), identities)
        targets = aliases or (strong if not possible else [])
        if len(targets) == 1 and targets[0] not in right:
            canonical = targets[0]
            identities[canonical]['ids'] = sorted(set(identities[canonical]['ids'] + [cid]))
            right[canonical] = {**right.pop(cid), 'Candidate ID': canonical}
        elif strong or possible or aliases:
            conflicts.append({'candidate_id': cid, 'field': '__identity__', 'integrated': right.pop(cid),
                              'matches': sorted(set(strong + possible + aliases)), 'reason': 'Unresolved working-copy identity'})
    prior_conflicts = {(c['candidate_id'], c['field']): c for c in state.get('conflicts', []) if c['field'] != '__identity__'}
    for cid in list(left):
        alias_matches = [canonical for canonical, identity in identities.items() if cid in identity['ids']]
        if len(alias_matches) == 1 and cid not in old and alias_matches[0] not in left:
            canonical = alias_matches[0]
            left[canonical] = {**left.pop(cid), 'Candidate ID': canonical}
            master_before[canonical] = master_before.get(cid, old.get(canonical, {}))
            continue
        if cid in old or cid in right:
            continue
        row = left[cid]
        candidates = list(right.values()) or list(old.values())
        for candidate in candidates:
            _learn(identities, candidate)
        incoming = {'Search Result URL': row.get('Original Search URL', ''),
                    'Official Website (manual)': row.get('Official Website', ''),
                    'Company Name (manual)': row.get('Company / Group', ''), 'Country': row.get('Target Country', '')}
        strong, possible = _matches(incoming, candidates, identities)
        if len(strong) == 1 and not possible and strong[0] not in left:
            canonical = strong[0]
            identities[canonical]['ids'] = sorted(set(identities[canonical]['ids'] + [cid]))
            left[canonical] = {**left.pop(cid), 'Candidate ID': canonical}
            master_before[canonical] = old.get(canonical, right.get(canonical, {}))
        elif strong or possible:
            conflicts.append({'candidate_id': cid, 'field': '__identity__', 'manual': left.pop(cid),
                              'matches': sorted(set(strong + possible)), 'reason': 'Unresolved manual identity'})
    result = []
    for cid in dict.fromkeys(list(right) + list(old) + list(left)):
        m, w = left.get(cid), right.get(cid)
        previous = old.get(cid, {})
        if m is None:
            result.append(dict(w if w is not None else previous))
            conflicts += [c for (cc, _), c in prior_conflicts.items() if cc == cid]
            continue
        if w is None:
            w = previous or m
        merged = {}
        for field in dict.fromkeys(list(previous) + list(w) + list(m)):
            mv, wv = m.get(field, previous.get(field, '')), w.get(field, previous.get(field, ''))
            mb = master_before.get(cid, previous).get(field, '')
            wb = previous.get(field, '')
            m_changed, w_changed = field in m and mv != mb, field in w and wv != wb
            prior = prior_conflicts.get((cid, field))
            if prior and not (mv == wv and (m_changed or w_changed)):
                # The exported base is not a human rejection of a stored alternative.
                if wv == wb:
                    wv = prior['integrated']
                if mv == mb:
                    mv = prior['manual']
            if mv == wv:
                value = mv
            elif prior or (cid not in old and mv and wv) or (m_changed and w_changed):
                value = previous.get(field, wv)
                conflicts.append({'candidate_id': cid, 'field': field, 'base': value, 'manual': mv,
                                  'integrated': wv, 'reason': 'Set both copies to the same value to resolve'})
            elif m_changed:
                value = mv
            else:
                value = wv
            merged[field] = value
        result.append(merged)
    resolved_aliases = {alias for i in identities.values() for alias in i['ids']}
    pending_ids = {c['candidate_id'] for c in conflicts if c['field'] == '__identity__'}
    conflicts += [c for c in state.get('conflicts', []) if c['field'] == '__identity__'
                  and c['candidate_id'] not in resolved_aliases | pending_ids]
    return result, conflicts


def _legacy_report(path, demo):
    if not path.exists():
        return []
    try:
        report = json.loads(path.read_text(encoding='utf-8-sig'))
        if not isinstance(report, dict) or report.get('demo') != demo:
            raise ValueError('Invalid report schema or DEMO mode mismatch')
        history = report.get('provenance', report.get('events', []))
        if not isinstance(history, list) or any(not isinstance(e, dict) for e in history):
            raise ValueError('Invalid report provenance')
        return redact(history)
    except json.JSONDecodeError as exc:
        raise ValueError('Invalid integration report JSON') from exc


def integrate(discovery: Path, review: Path, out: Path, *, demo=False, identity_map=None):
    if any(p.name.casefold().startswith('.env') or p.resolve().name.casefold().startswith('.env') for p in (discovery, review)):
        raise ValueError('Credential files cannot be candidate data')
    target = out / 'candidate_review.csv'
    reserved = [target, out / 'integration_report.json', out / '.integration.lock']
    if any(p.resolve() in {review.resolve(), discovery.resolve()} for p in reserved):
        raise ValueError('Outputs must not overwrite inputs')
    if any((out / '.integration').resolve() in p.resolve().parents for p in (review, discovery)):
        raise ValueError('Inputs must not be inside internal state')
    namespace = {'candidates.csv': 'discovery', 'candidates_demo.csv': 'discovery-demo'}.get(discovery.name.lower())
    if namespace and (discovery.parent/'.exports'/namespace).exists():
        with writer_lock(discovery.parent):
            ExportBundle(discovery.parent, namespace).recover()
    source_bytes = discovery.read_bytes()
    manual_bytes = review.read_bytes() if review.exists() else None
    _, sources = read_csv(discovery, DISCOVERY_REQUIRED)
    if review.exists():
        fields, manual = read_csv(review, REVIEW_REQUIRED)
    else:
        fields, manual = REVIEW_FIELDS[:], []
    validate_rows(manual, demo=demo)
    manual = [redact(r) for r in manual]
    with writer_lock(out):
        store = Store(out)
        state = store.load()
        store.clean_temps()
        if state is None:
            state = {'schema': 1, 'demo': demo, 'rows': [], 'manual_snapshot': [],
                     'identities': {}, 'conflicts': [], 'provenance': _legacy_report(out / 'integration_report.json', demo)}
        if state.get('demo') != demo:
            raise ValueError('Use separate DEMO and live directories')
        for key in ('rows', 'manual_snapshot', 'conflicts', 'provenance'):
            if not isinstance(state.get(key), list):
                raise ValueError(f'Invalid state collection: {key}')
        if not isinstance(state.get('identities'), dict):
            raise ValueError('Invalid identity registry')
        validate_rows(state['rows'], demo=demo)
        validate_rows(state['manual_snapshot'], demo=demo)
        for identity in state['identities'].values():
            if not isinstance(identity, dict) or any(not isinstance(identity.get(k), list)
                    or any(not isinstance(v, str) for v in identity[k]) for k in ('search', 'official', 'ids')):
                raise ValueError('Invalid identity aliases')
        if any(not isinstance(e, dict) or not isinstance(e.get('source', {}), dict) for e in state['provenance']):
            raise ValueError('Invalid provenance event')
        working = []
        working_bytes = target.read_bytes() if target.exists() else None
        if target.exists():
            previous_fields, working = read_csv(target, REVIEW_REQUIRED)
            fields = list(dict.fromkeys(fields + previous_fields))
            old = {r['Candidate ID']: r for r in state['rows']}
            for row in working:
                canonical = old.get(row['Candidate ID'], {})
                projection = project_row(canonical)
                for k in row:
                    if k in projection and row[k] == projection[k]:
                        row[k] = canonical[k]
        validate_rows(working, demo=demo)
        identities = state['identities']
        for row in (state['rows'] if state['rows'] else (working or manual)):
            _learn(identities, row)
        known = {r['Candidate ID'] for r in state['rows'] + working + manual}
        for event in state['provenance']:
            cid = event.get('candidate_id')
            if event.get('action') in {'new', 'existing'} and cid in known:
                historical = event.get('source', {})
                canonical = next((r for r in state['rows'] + working + manual if r['Candidate ID'] == cid), {})
                contradictory = any(historical.get(origin, '').strip() and canonical.get(target, '').strip()
                                    and _text(historical[origin]) != _text(canonical[target])
                                    for origin, target in [('Country', 'Target Country'), ('Company Name (manual)', 'Company / Group')])
                if not contradictory:
                    _learn(identities, {'Candidate ID': cid}, historical)
        baseline, conflicts = _reconcile(manual, [redact(r) for r in working], state, identities, identity_map)
        validate_rows(baseline, demo=demo)
        for row in baseline:
            _learn(identities, row)
        rows, events = merge_candidates(sources, baseline, demo=demo, identities=identities)
        fields = list(dict.fromkeys(fields + [k for r in rows for k in r] + REVIEW_FIELDS))
        provenance = {json.dumps(e, sort_keys=True, ensure_ascii=False): e for e in state['provenance'] + events}
        issues = [{'candidate_id': r['Candidate ID'], **issue} for r in rows for issue in _url_issues(r)]
        alias_owners = {}
        for cid, identity in identities.items():
            for role in ('search', 'official'):
                for alias in identity[role]:
                    alias_owners.setdefault((role, alias), []).append(cid)
        duplicate_warnings = [{'role': role, 'url': alias, 'candidate_ids': owners}
                              for (role, alias), owners in alias_owners.items() if len(owners) > 1]
        report = {'version': '2.2.1', 'demo': demo, 'counts': {action: sum(e['action'] == action for e in events)
                  for action in ('new', 'existing', 'ambiguous', 'rejected')}, 'events': events,
                  'provenance': list(provenance.values()), 'conflicts': conflicts, 'url_issues': issues,
                  'identity_warnings': duplicate_warnings}
        stream = io.StringIO(newline='')
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(project_row({k: r.get(k, '') for k in fields}) for r in rows)
        state.update({'rows': rows, 'manual_snapshot': manual, 'conflicts': conflicts,
                      'previous_csv_hash': hashlib.sha256(working_bytes).hexdigest() if working_bytes is not None else None,
                      'provenance': list(provenance.values())})
        if (target.read_bytes() if target.exists() else None) != working_bytes:
            raise ValueError('Working CSV changed during integration; retry without overwriting edits')
        if discovery.read_bytes() != source_bytes or (review.read_bytes() if review.exists() else None) != manual_bytes:
            raise ValueError('Input changed during integration; retry with a stable snapshot')
        report['generation'] = store.commit(state, stream.getvalue().encode('utf-8-sig'), report)
        return target, report
