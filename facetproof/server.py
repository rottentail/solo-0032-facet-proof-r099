import argparse
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from .application import Application
from .errors import Fault, require
from .routing import route_request
from .values import canonical, parse_json

LOG = logging.getLogger("facetproof")

def handler_for(app):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(15)
        def send_payload(self, status, data, mime):
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            self.wfile.write(data)
        def handle_request(self):
            try:
                require(not self.headers.get("Transfer-Encoding"), "encoding", "Chunked bodies unsupported")
                try: length = int(self.headers.get("Content-Length", "0"))
                except ValueError as exc: raise Fault("length", "Invalid Content-Length") from exc
                require(0 <= length <= 4*1024*1024, "body_too_large", "Body limit is 4 MiB", 413)
                if length:
                    require(self.headers.get_content_type() == "application/json", "content_type", "Expected JSON", 415)
                    raw = self.rfile.read(length)
                    require(len(raw) == length, "body", "Incomplete body")
                    data = parse_json(raw)
                else: data = {}
                value = route_request(app, self.command, self.path, data)
                raw, mime = value if isinstance(value, tuple) else (canonical(value).encode(), "application/json; charset=utf-8")
                self.send_payload(200, raw, mime)
            except Fault as exc:
                self.send_payload(exc.status, canonical(exc.as_dict()).encode(), "application/json; charset=utf-8")
            except (BrokenPipeError, ConnectionResetError, TimeoutError): self.close_connection = True
            except Exception:
                LOG.exception("request failed")
                self.send_payload(500, b'{"error":"internal"}', "application/json")
        do_GET = handle_request
        do_POST = handle_request
        do_PUT = handle_request
    return Handler

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".facetproof")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8796)
    args = parser.parse_args()
    app = Application(args.root)
    app.exposures.recover()
    server = ThreadingHTTPServer((args.host, args.port), handler_for(app))
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()

if __name__ == "__main__": main()
