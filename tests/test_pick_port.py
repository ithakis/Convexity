"""server._pick_port must bind like the real server does.

The probe used to bind without SO_REUSEADDR while HTTPServer binds with it, so
for ~30 s after any restart (the port's last connections in TIME_WAIT) the app
moved off 8765 although the real bind would have worked.
"""

import contextlib
import http.server
import socket
import threading
import urllib.request

from convexity import server


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_port_in_time_wait_is_reused():
    port = _free_port()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), http.server.BaseHTTPRequestHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    # A 501 is fine: the connection happened, the server closed it.
    with contextlib.suppress(Exception):
        urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5)
    srv.shutdown()
    srv.server_close()  # the server-side socket of that connection is now in TIME_WAIT
    assert server._pick_port(port) == port


def test_port_someone_listens_on_is_skipped():
    with socket.socket() as busy:
        busy.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        assert server._pick_port(port) != port
