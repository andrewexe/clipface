"""Installed-app OAuth: a one-shot loopback listener that receives Google's redirect.

The browser is sent to Google with a PKCE challenge and a random `state`. Google sends
it back to http://127.0.0.1:<port>/ with a code. The listener accepts exactly one
redirect whose state matches, then shuts down. It listens on loopback only, for five
minutes at most.
"""

from __future__ import annotations

import hmac
import logging
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

log = logging.getLogger(__name__)

TIMEOUT_S = 300

_PAGE = """<!doctype html><meta charset="utf-8"><title>Clipper</title>
<body style="font-family:system-ui;background:#111318;color:#e7e9ee;text-align:center;
padding-top:15vh">
<h2>{heading}</h2><p>{detail}</p></body>"""


class OAuthListener:
    def __init__(
        self, state: str, on_code: Callable[[str, str], str | None], path: str = ""
    ) -> None:
        """`on_code(code, redirect_uri)` finishes the sign-in and returns an error message
        to show, or None on success."""
        self._state = state
        self._on_code = on_code
        self._server = HTTPServer(("127.0.0.1", 0), self._handler())
        self._server.timeout = 1.0
        self._path = path  # some platforms want a fixed path, e.g. "/callback/"
        self.redirect_uri = f"http://127.0.0.1:{self._server.server_address[1]}{path}"
        self.finished = threading.Event()
        self.error: str | None = None
        self._thread = threading.Thread(target=self._serve, name="oauth-listener", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def cancel(self) -> None:
        self.error = self.error or "cancelled"
        self.finished.set()

    def _serve(self) -> None:
        waited = 0.0
        try:
            while not self.finished.is_set() and waited < TIMEOUT_S:
                self._server.handle_request()  # returns after `timeout` if nothing came
                waited += self._server.timeout
            if not self.finished.is_set():
                self.error = "timed out waiting for the sign-in"
                self.finished.set()
        finally:
            self._server.server_close()

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        listener = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args) -> None:  # never log the redirect: it holds the code
                pass

            def _page(self, status: int, heading: str, detail: str) -> None:
                body = _PAGE.format(heading=heading, detail=detail).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802 - name required by BaseHTTPRequestHandler
                parts = urlsplit(self.path)
                wanted = listener._path.rstrip("/")
                if parts.path.rstrip("/") != wanted:
                    return self._page(404, "Not found", "")
                query = parse_qs(parts.query)
                state = query.get("state", [""])[0]
                if not hmac.compare_digest(state.encode(), listener._state.encode()):
                    return self._page(
                        400, "That sign-in link is not valid.", "Start again from Clipper."
                    )
                if listener.finished.is_set():
                    return self._page(400, "This sign-in was already used.", "")
                if "error" in query:
                    listener.error = "the sign-in was cancelled or refused"
                    listener.finished.set()
                    return self._page(200, "Sign-in cancelled.", "You can close this tab.")
                code = query.get("code", [""])[0]
                if not code:
                    return self._page(
                        400, "No sign-in code was received.", "Start again from Clipper."
                    )
                try:
                    listener.error = listener._on_code(code, listener.redirect_uri)
                except Exception:
                    log.exception("finishing the sign-in failed")
                    listener.error = "the sign-in could not be completed"
                listener.finished.set()
                if listener.error:
                    return self._page(200, "Sign-in failed.", listener.error)
                return self._page(
                    200, "Account connected.", "You can close this tab and go back to Clipper."
                )

        return Handler
