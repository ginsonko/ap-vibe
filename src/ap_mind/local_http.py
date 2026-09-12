"""Loopback services do not need reverse DNS to bind their numeric address."""
from http.server import ThreadingHTTPServer
from socketserver import TCPServer


class LocalThreadingHTTPServer(ThreadingHTTPServer):
    def server_bind(self):
        # HTTPServer.server_bind calls getfqdn(), which may wait on external DNS
        # even for loopback on macOS/VPN networks. No routing decision uses that
        # hostname: keep the actual bound address for diagnostics and headers.
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]
