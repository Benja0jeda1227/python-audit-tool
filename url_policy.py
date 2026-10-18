"""Shared offline URL validation, comparison keys and export redaction.

Validation is not reachability or business verification. DNS checks belong to
the transport, immediately before a connection is made.
"""
from __future__ import annotations

import ipaddress
import re
from urllib.parse import parse_qsl, urlencode, unquote_plus, urlsplit, urlunsplit

TRACKING = {'fbclid', 'gclid', 'msclkid', 'dclid'}
SENSITIVE = re.compile(r'(?:password|passwd|secret|token|authorization|api[_ -]?key|private[_ -]?key|credential|signature|^sig$)', re.I)
URL_FIELDS = {'Official Website', 'Original Search URL', 'Search Result URL',
              'Official Website (manual)', 'Public Contact Page', 'Public Contact Page (manual)',
              'Evidence URL (manual)', 'Products Evidence URL', 'Manufacturing Evidence URL'}


def public_ip(value: str) -> bool:
    ip = ipaddress.ip_address(value)
    return ip.is_global and not (ip.is_multicast or ip.is_reserved or ip.is_loopback
                                or ip.is_link_local or ip.is_unspecified or ip.is_private)


def public_url(value: str) -> str:
    """Public HTTP(S) syntax only; reject credentials and sensitive queries."""
    try:
        value = value.strip()
        if not value or re.search(r'[\x00-\x20\x7f\\]', value):
            return ''
        p = urlsplit(value)
        host = (p.hostname or '').rstrip('.').encode('idna').decode('ascii').lower()
        if p.scheme not in {'http', 'https'} or not host or p.username is not None or p.password is not None:
            return ''
        if p.port is not None and not 1 <= p.port <= 65535:
            return ''
        if host == 'localhost' or host.endswith(('.localhost', '.local', '.internal', '.home', '.lan')):
            return ''
        try:
            if not public_ip(host):
                return ''
        except ValueError:
            if '.' not in host or re.fullmatch(r'[\d.]+', host):
                return ''
            if any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
                   for label in host.split('.')):
                return ''
            if len(host) > 253:
                return ''
        if any(SENSITIVE.search(k) for k, _ in parse_qsl(p.query, keep_blank_values=True)):
            return ''
        if '=' in p.fragment and any(SENSITIVE.search(k) for k, _ in parse_qsl(p.fragment, keep_blank_values=True)):
            return ''
        if re.search(r'%(?![0-9a-fA-F]{2})', value):
            return ''
        return value
    except (ValueError, UnicodeError, AttributeError):
        return ''


def normalize_url(value: str) -> str:
    value = public_url(value)
    if not value:
        return ''
    p = urlsplit(value)
    host = p.hostname.rstrip('.').encode('idna').decode('ascii').lower().removeprefix('www.')
    netloc = f'[{host}]' if ':' in host else host
    if p.port is not None and (p.scheme, p.port) not in {('http', 80), ('https', 443)}:
        netloc += f':{p.port}'
    # Preserve query order, duplicates and meaningful values; drop tracking only.
    query = []
    for component in p.query.split('&'):
        try:
            key = unquote_plus(component.split('=', 1)[0], errors='strict').lower()
        except UnicodeError:
            key = ''
        if not key.startswith('utm_') and key not in TRACKING:
            query.append(component)
    return urlunsplit((p.scheme, netloc, p.path.rstrip('/') or '/', '&'.join(query), ''))


def redact(value):
    """Remove recognizable credentials before logging or persisting provenance."""
    if isinstance(value, dict):
        return {str(k): '[REDACTED]' if SENSITIVE.search(str(k)) else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if not isinstance(value, str):
        return value
    def clean_link(match):
        link = match.group(0)
        try:
            p = urlsplit(link)
            parts = [p.query, p.fragment]
            for i, part in enumerate(parts):
                if '=' in part and any(SENSITIVE.search(k) for k, _ in parse_qsl(part, keep_blank_values=True)):
                    parts[i] = urlencode([(k, '[REDACTED]' if SENSITIVE.search(k) else v)
                                          for k, v in parse_qsl(part, keep_blank_values=True)])
            link = urlunsplit((p.scheme, p.netloc, p.path, parts[0], parts[1]))
        except ValueError:
            pass
        return link
    value = re.sub(r'https?://[^\s<>\"\']+', clean_link, value, flags=re.I)
    value = re.sub(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
                   '[REDACTED]', value, flags=re.S)
    value = re.sub(r'(?i)(authorization\s*[:=]\s*)(?:bearer|basic)\s+\S+', r'\1[REDACTED]', value)
    value = re.sub(r'(https?://)[^\s/<>\"@]+@', r'\1[REDACTED]@', value, flags=re.I)
    value = re.sub(r'([?&](?:[^=&\s]*(?:token|password|secret|api[_ -]?key|credential)[^=&\s]*)=)[^&\s<>\"\']*',
                   r'\1[REDACTED]', value, flags=re.I)
    value = re.sub(r'\b(?:tvly-[A-Za-z0-9_-]{16,}|sk-(?:proj-)?[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16})\b',
                   '[REDACTED]', value)
    value = re.sub(r'(?i)((?:password|passwd|secret|api[_ -]?key|token|authorization|credential|signature)\s*[:=]\s*)[^\s,;]+',
                   r'\1[REDACTED]', value)
    return value


def safe_cell(value: str) -> str:
    return "'" + value if value.lstrip().startswith(('=', '+', '-', '@')) or value.startswith(('\t', '\r', '\n')) else value


def project_row(row: dict[str, str]) -> dict[str, str]:
    """Safe spreadsheet projection; canonical values live in JSON state."""
    return {k: safe_cell(redact(v)) if k not in URL_FIELDS or not v.strip() or public_url(v) else ''
            for k, v in row.items()}
