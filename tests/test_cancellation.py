from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
import unittest

from pycoder.config import Config
from pycoder.events import Cancelled
from pycoder.transport import Client


class CancellationTests(unittest.TestCase):
    def test_cancel_blocked_sse_read_and_reuse_client(self):
        sent = threading.Event()
        release = threading.Event()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n')
                self.wfile.flush()
                sent.set()
                release.wait(3)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        httpd.daemon_threads = True
        server = threading.Thread(target=httpd.serve_forever, daemon=True)
        server.start()
        client = Client(Config(model="test", base_url=f"http://127.0.0.1:{httpd.server_port}/v1"), "")
        outcome = []
        def request():
            try:
                client.complete([{"role": "user", "content": "hi"}])
            except Cancelled:
                outcome.append("cancelled")
        thread = threading.Thread(target=request)
        try:
            thread.start()
            self.assertTrue(sent.wait(2))
            # The HTTP/1.0 connection releases conn.sock; cancellation must also
            # reach HTTPResponse's socket while readline is blocked.
            time.sleep(0.1)
            client.cancel()
            thread.join(1)
            self.assertFalse(thread.is_alive())
            self.assertEqual(outcome, ["cancelled"])
            client.cancelled.clear()
            client.check_cancelled()
        finally:
            release.set()
            client.cancel()
            thread.join(3)
            httpd.shutdown()
            httpd.server_close()
            server.join()
