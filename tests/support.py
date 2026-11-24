from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading


def reply(content="done", calls=None, finish=None, usage=None):
    message = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = calls
    return {"choices": [{"index": 0, "message": message,
                         "finish_reason": finish or ("tool_calls" if calls else "stop")}],
            "usage": usage or {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}}


def call(name, arguments, call_id="call_1"):
    return {"id": call_id, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments)}}


@contextmanager
def server(responder):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.respond(body)

        def do_GET(self):
            self.respond(None)

        def respond(self, body):
            requests.append({"method": self.command, "path": self.path, "body": body, "headers": dict(self.headers)})
            status, content_type, response, extra = responder(requests)
            data = response.encode() if isinstance(response, str) else json.dumps(response).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            for key, value in extra.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}/v1", requests
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


def json_response(body):
    return 200, "application/json", body, {}
