"""Direct HTTP transport: validate DNS and pin each connection to a public IP.

Uses urllib's bounded redirects and normal TLS certificate validation. Proxies
are intentionally disabled so the destination checked is the destination used.
"""
from __future__ import annotations

import http.client
import socket
from urllib.parse import urlsplit
from urllib.error import URLError
from urllib.request import (HTTPHandler, HTTPSHandler, HTTPRedirectHandler,
                            ProxyHandler, Request, build_opener)

from url_policy import public_url, public_ip


def public_addresses(host, port):
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        if not addresses or any(not public_ip(a[4][0]) for a in addresses):
            raise URLError('Destination resolves to a non-public address')
        return addresses
    except (ValueError, OSError) as exc:
        raise URLError('Public DNS resolution failed') from exc


def _connect(connection):
    addresses = public_addresses(connection.host, connection.port)
    last = None
    for family, kind, proto, _, address in addresses:
        sock = socket.socket(family, kind, proto)
        try:
            sock.settimeout(connection.timeout)
            sock.connect(address)
            if not public_ip(sock.getpeername()[0]):
                raise URLError('Non-public connection peer')
            return sock
        except (OSError, URLError) as exc:
            sock.close()
            last = exc
    raise URLError('Public connection failed') from last


class PinnedHTTPConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = _connect(self)


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        self.sock = _connect(self)
        try:
            self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)
        except BaseException:
            self.sock.close()
            raise


class PublicHTTPHandler(HTTPHandler):
    def http_open(self, request):
        return self.do_open(PinnedHTTPConnection, request)


class PublicHTTPSHandler(HTTPSHandler):
    def https_open(self, request):
        return self.do_open(PinnedHTTPSConnection, request, context=self._context)


class PublicRedirectHandler(HTTPRedirectHandler):
    def __init__(self, policy=None):
        self.policy = policy

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not public_url(newurl) or (req.type == 'https' and urlsplit(newurl).scheme == 'http'):
            raise URLError('Unsafe redirect rejected before connection')
        if self.policy is not None and not self.policy(newurl):
            raise URLError('Redirect not allowed by target robots policy')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def urlopen(request, timeout=10, context=None, *, redirect_policy=None):
    address = request.full_url if isinstance(request, Request) else request
    if not public_url(address):
        raise URLError('Invalid or unsafe HTTP URL')
    opener = build_opener(ProxyHandler({}), PublicHTTPHandler(), PublicHTTPSHandler(context=context),
                          PublicRedirectHandler(redirect_policy))
    return opener.open(request, timeout=timeout)
