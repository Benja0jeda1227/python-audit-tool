"""Transport security tests use mocked DNS, sockets, HTTP and TLS only."""
import http.client
from email.message import Message
from urllib.error import HTTPError, URLError
from urllib.request import Request
from unittest.mock import Mock, patch

import safe_http
import website_audit as base
import website_audit_v2_1 as audit
from test_v22_regressions import OfflineCase


def addresses(*ips):
    return [(2, 1, 6, '', (ip, 443)) for ip in ips]


class Response:
    def __init__(self, data=b'<main><p>We manufacture cheese in Chile.</p></main>',
                 url='https://public.example/', mime='text/html; charset=utf-8'):
        self.data, self.url = data, url
        self.headers = Message()
        self.headers['Content-Type'] = mime

    def __enter__(self): return self
    def __exit__(self, *args): pass
    def getcode(self): return 200
    def geturl(self): return self.url
    def read(self, size): return self.data[:size]


class TransportTests(OfflineCase):
    def test_private_or_mixed_dns_is_rejected_before_socket_creation(self):
        for ips in [('127.0.0.1',), ('93.184.216.34', '10.0.0.1'), ('169.254.169.254',), ('224.0.0.1',)]:
            with patch.object(safe_http.socket, 'getaddrinfo', return_value=addresses(*ips)), patch.object(safe_http.socket, 'socket') as create:
                with self.assertRaises(URLError): safe_http._connect(Mock(host='public.example', port=443))
                create.assert_not_called()

    def test_pinned_public_address_and_peer_checked(self):
        connection = Mock(host='public.example', port=443, timeout=3)
        sock = Mock(); sock.getpeername.return_value = ('93.184.216.34', 443)
        with patch.object(safe_http.socket, 'getaddrinfo', return_value=addresses('93.184.216.34')) as dns, patch.object(safe_http.socket, 'socket', return_value=sock):
            self.assertIs(safe_http._connect(connection), sock)
            self.assertEqual(dns.call_count, 1)
            sock.connect.assert_called_once_with(('93.184.216.34', 443))
            sock.settimeout.assert_called_once_with(3)

    def test_private_peer_closes_socket(self):
        sock = Mock(); sock.getpeername.return_value = ('127.0.0.1', 443)
        with patch.object(safe_http.socket, 'getaddrinfo', return_value=addresses('93.184.216.34')), patch.object(safe_http.socket, 'socket', return_value=sock):
            with self.assertRaises(URLError): safe_http._connect(Mock(host='public.example', port=443, timeout=3))
            sock.close.assert_called_once()

    def test_https_retains_hostname_for_certificate_verification(self):
        connection = safe_http.PinnedHTTPSConnection('public.example', timeout=3)
        sock = Mock()
        connection._context = Mock()
        with patch.object(safe_http, '_connect', return_value=sock): connection.connect()
        connection._context.wrap_socket.assert_called_once_with(sock, server_hostname='public.example')

    def test_redirects_validate_destination_before_following(self):
        handler = safe_http.PublicRedirectHandler()
        req = Request('https://public.example/')
        for url in ['http://127.0.0.1/', 'https://10.0.0.1/', 'file:///tmp/a', 'http://public.example/', 'https://[']:
            with self.assertRaises(URLError): handler.redirect_request(req, None, 302, '', {}, url)
        redirect = handler.redirect_request(req, None, 302, '', {}, 'https://other.example/')
        self.assertEqual(redirect.full_url, 'https://other.example/')
        with self.assertRaises(URLError):
            handler.redirect_request(Request('HTTPS://public.example/'), None, 302, '', {}, 'http://other.example/')

    def test_redirect_rechecks_target_robots(self):
        handler = safe_http.PublicRedirectHandler(lambda url: False)
        with self.assertRaises(URLError): handler.redirect_request(Request('https://public.example/'), None, 302, '', {}, 'https://other.example/')

    def test_unsafe_initial_url_never_builds_opener(self):
        with patch.object(safe_http, 'build_opener') as build:
            with self.assertRaises(URLError): safe_http.urlopen('http://127.0.0.1/')
            build.assert_not_called()

    def test_dns_failures_are_controlled(self):
        with patch.object(safe_http.socket, 'getaddrinfo', side_effect=OSError('fixture DNS error')):
            with self.assertRaises(URLError): safe_http.public_addresses('public.example', 443)


class AuditFetcherTests(OfflineCase):
    def fetch(self, response=None, error=None):
        fetcher = audit.CleanPublicFetcher(timeout=3, delay=1)
        with patch.object(fetcher, 'robots_permission', return_value=(True, 'robots_checked')), patch.object(fetcher, 'pause'), patch.object(audit, 'urlopen', return_value=response or Response(), side_effect=error):
            return fetcher.fetch('https://public.example/')

    def test_connection_timeout_http_and_protocol_errors_are_per_page(self):
        for exc, status in [(TimeoutError(), 'network_error'), (URLError('fixture'), 'network_error'),
                            (HTTPError('https://public.example/', 403, '', {}, None), 'http_403'),
                            (http.client.BadStatusLine('fixture'), 'invalid_response')]:
            self.assertEqual(self.fetch(error=exc).status, status)

    def test_invalid_mime_oversize_charset_and_redirect_are_reported(self):
        cases = [(Response(mime='application/pdf'), 'non_html'),
                 (Response(data=b'x'*(audit.MAX_PAGE_BYTES+1)), 'too_large'),
                 (Response(mime='text/html; charset=not-a-real-codec'), 'encoding_error'),
                 (Response(url='http://127.0.0.1/'), 'unsafe_redirect')]
        for response, status in cases: self.assertEqual(self.fetch(response).status, status)

    def test_robots_unknown_or_disallow_never_fetches_page(self):
        fetcher = audit.CleanPublicFetcher(timeout=3, delay=1)
        for status in ['robots_checked', 'robots_fetch_error']:
            with patch.object(fetcher, 'robots_permission', return_value=(False, status)), patch.object(audit, 'urlopen') as page:
                fetcher.fetch('https://public.example/'+status)
                page.assert_not_called()

    def test_robots_html_or_oversize_are_not_permissions(self):
        for response, expected in [(Response(data=b'<html>blocked</html>'), 'robots_invalid_response'),
                                   (Response(data=b'x'*150001), 'robots_too_large')]:
            fetcher = audit.CleanPublicFetcher(timeout=3, delay=1)
            with patch.object(base, 'urlopen', return_value=response), patch.object(fetcher, 'pause'):
                allowed, status = fetcher.robots_permission('https://public.example/')
            self.assertFalse(allowed); self.assertEqual(status, expected)

    def test_malformed_url_never_fetches_and_other_candidates_continue(self):
        rows = [{'Candidate ID': 'A', 'Public Contact Page': 'https://['},
                {'Candidate ID': 'B', 'Official Website': 'https://public.example/'}]
        fake = Mock(); fake.fetch.return_value = base.FetchResult('https://public.example/', 'network_error')
        results, _, _, _ = audit.audit(rows, fake)
        self.assertEqual(len(results), 2)
        self.assertEqual(fake.fetch.call_count, 1)

    def test_external_redirect_is_not_attributed_to_original_official_site(self):
        row = {'Candidate ID': 'A', 'Company / Group': 'Official', 'Target Country': 'Chile',
               'Official Website': 'https://official.example/'}
        fake = Mock()
        fake.fetch.return_value = base.FetchResult('https://external.example/', 'ok', '200', 'External',
                                                   'We operate dairy manufacturing plants in Chile.')
        _, pages, evidence, _ = audit.audit([row], fake)
        self.assertEqual(pages[0]['Final URL'], 'https://external.example/')
        self.assertEqual(pages[0]['First Party Domain'], 'No; verify attribution')
        self.assertFalse(any(e.kind == 'OWN_MANUFACTURING_CLAIM' for e in evidence))

    def test_v21_multilingual_manufacturing_and_trader_regression(self):
        cases = [('Chile', 'Fabricamos queso y mantequilla en nuestra planta de producci\u00f3n en Chile.'),
                 ('France', 'Nous fabriquons du fromage et du beurre dans nos usines en France.'),
                 ('United States', 'We operate dairy manufacturing plants in the United States.')]
        for country, text in cases:
            evidence = audit.extract_evidence('X', 'Fixture', 'Homepage', 'https://public.example/', text, country, True)
            self.assertTrue(any(e.kind == 'OWN_MANUFACTURING_CLAIM' for e in evidence))
        evidence = audit.extract_evidence('X', 'Trader', 'Homepage', 'https://public.example/',
                                         'We are a global dairy commodities trader. Our suppliers manufacture milk powder.', 'Chile', True)
        self.assertTrue(any(e.kind == 'THIRD_PARTY_MANUFACTURING' for e in evidence))
        self.assertFalse(any(e.kind == 'OWN_MANUFACTURING_CLAIM' for e in evidence))
