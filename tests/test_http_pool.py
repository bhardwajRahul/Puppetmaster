"""Provider keep-alive pool: reuse only provably healthy, fully read connections."""
import json
import socket
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from puppetmaster import http_pool


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    connections = set()

    def log_message(self, *a):
        pass

    def do_POST(self):
        _Handler.connections.add(self.client_address)
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path == "/error":
            out, status = b'{"error": "nope"}', 429
        else:
            out, status = json.dumps({"echo": body.decode()}).encode(), 200
        self.send_response(status)
        if self.path == "/close":
            self.send_header("Connection", "close")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self):
        _Handler.connections.add(self.client_address)
        self.send_response(200)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for i in range(3):
            line = f"data: {i}\n\n".encode()
            self.wfile.write(b"%x\r\n%s\r\n" % (len(line), line))
        self.wfile.write(b"0\r\n\r\n")


class HttpPoolTests(unittest.TestCase):
    def setUp(self):
        http_pool.reset()
        _Handler.connections = set()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        http_pool.reset()

    def post(self, path, data=b"{}"):
        return urllib.request.Request(self.base + path, data=data, method="POST",
                                      headers={"Content-Type": "application/json"})

    def test_sequential_requests_reuse_one_connection(self):
        for i in range(4):
            with http_pool.urlopen(self.post("/x", str(i).encode()), timeout=5) as resp:
                self.assertEqual(json.loads(resp.read())["echo"], str(i))
        self.assertEqual(len(_Handler.connections), 1)

    def test_stream_read_to_end_is_reused_and_abandoned_stream_is_not(self):
        with http_pool.urlopen(urllib.request.Request(self.base + "/s"), timeout=5) as resp:
            self.assertEqual([l for l in resp if l.strip()], [b"data: 0\n", b"data: 1\n", b"data: 2\n"])
        with http_pool.urlopen(urllib.request.Request(self.base + "/s"), timeout=5) as resp:
            resp.readline()
        with http_pool.urlopen(urllib.request.Request(self.base + "/s"), timeout=5) as resp:
            resp.read()
        self.assertEqual(len(_Handler.connections), 2)

    def test_closed_and_shut_down_sockets_are_discarded(self):
        with http_pool.urlopen(self.post("/close"), timeout=5) as resp:
            resp.read()
        with http_pool.urlopen(self.post("/x"), timeout=5) as resp:
            sock = resp.fp.raw._sock
            resp.read()
        sock.shutdown(socket.SHUT_RD)
        with http_pool.urlopen(self.post("/x"), timeout=5) as resp:
            resp.read()
        self.assertEqual(len(_Handler.connections), 3)

    def test_http_errors_match_urllib(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            http_pool.urlopen(self.post("/error"), timeout=5)
        self.assertEqual(ctx.exception.code, 429)
        self.assertEqual(json.loads(ctx.exception.read()), {"error": "nope"})

    def test_a_replaced_urlopen_still_intercepts(self):
        with patch("urllib.request.urlopen", side_effect=RuntimeError("patched")):
            with self.assertRaises(RuntimeError):
                http_pool.urlopen(self.post("/x"), timeout=5)


if __name__ == "__main__":
    unittest.main()
