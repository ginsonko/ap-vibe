import socket
from http.server import BaseHTTPRequestHandler
from ap_mind.local_http import LocalThreadingHTTPServer


def test_loopback_bind_needs_no_reverse_dns(monkeypatch):
    def unavailable(*args):
        raise AssertionError('Loopback startup must not wait for DNS')
    monkeypatch.setattr(socket,'getfqdn',unavailable)
    with LocalThreadingHTTPServer(('127.0.0.1',0),BaseHTTPRequestHandler) as server:
        assert server.server_name=='127.0.0.1'
        assert server.server_port>0
