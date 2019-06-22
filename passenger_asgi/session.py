import os
import logging
from enum import IntEnum, auto
from typing import Optional, Union

from passenger_asgi.asgi_typing import Scope, Event


class ProtocolState(IntEnum):
    REQUIRES_HEADER_LENGTH = auto()
    HEADERS_INCOMING = auto()
    RECEIVING_DATA = auto()
    CLOSED = auto()


class PassengerPreloaderProtocol(object):
    pass


class PassengerSessionProtocol(object):
    HTTPS_VALUES = (b"on", b"1", b"true", b"yes")

    def __init__(self):
        self.sid = int.from_bytes(os.urandom(8), "big").__format__("x")
        self.logger = logging.getLogger(f"passenger_asgi.session.{self.sid}")
        self._state = ProtocolState.REQUIRES_HEADER_LENGTH
        self._required = 4

    def state_requires_bytes(self) -> int:
        """
        :return: 0 - Send what you have to .feed; >0 - Send *exactly* that many bytes to .feed
        """
        return self._required

    def is_closed(self) -> bool:
        return self._state == ProtocolState.CLOSED

    def feed(self, data: Optional[bytes]) -> Optional[Union[Event, Scope]]:
        if self._state == ProtocolState.CLOSED:
            return {'type': 'http.disconnect'}

        if data is None and self._state == ProtocolState.RECEIVING_DATA:
            self._state = ProtocolState.CLOSED
            return {'type': 'http.request', 'more_body': False}

        if self._state == ProtocolState.REQUIRES_HEADER_LENGTH:
            self._required = int.from_bytes(data, 'big')
            self._state = ProtocolState.HEADERS_INCOMING
            return None

        elif self._state == ProtocolState.HEADERS_INCOMING:
            self._required = 0
            self._state = ProtocolState.RECEIVING_DATA
            return self._received_full_event(data)

        elif self._state == ProtocolState.RECEIVING_DATA:
            return {'type': 'http.request', 'body': data, 'more_body': True}

        else:
            raise RuntimeError("Unknown state?!")

    def _received_full_event(self, data: bytes) -> Scope:
        list_headers = data.split(b"\0")
        list_headers = list(zip(list_headers[::2], list_headers[1::2]))
        d_list_headers = dict(list_headers)

        headers = [(h[5:].lower().replace(b"_", b"-"), v) for h, v in list_headers if h.startswith(b"HTTP_")]
        if b"CONTENT_LENGTH" in d_list_headers:
            headers.append((b"content-length", d_list_headers[b"CONTENT_LENGTH"]))
        if b"CONTENT_TYPE" in d_list_headers:
            headers.append((b"content-type", d_list_headers[b"CONTENT_TYPE"]))

        scope: Scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.1"},

            "http_version": d_list_headers[b"SERVER_PROTOCOL"].split(b"/")[1].decode("latin1"),
            "method":       d_list_headers[b"REQUEST_METHOD"].decode("latin-1"),
            "scheme":       "https" if d_list_headers.get(b"HTTPS", b"off") in self.HTTPS_VALUES else "http",

            "root_path":    d_list_headers.get(b"SCRIPT_NAME", b"").decode("latin-1"),
            "path":         d_list_headers.get(b"PATH_INFO", b"").decode("latin-1"),
            "raw_path":     d_list_headers.get(b"REQUEST_URI", b""),

            "query_string": d_list_headers.get(b"QUERY_STRING"),
            "headers":      headers,
            "client":       [
                d_list_headers.get(b"REMOTE_ADDR").decode("latin-1"),
                int(d_list_headers.get(b"REMOTE_PORT").decode("latin-1"))
            ],
            "server":       [
                d_list_headers.get(b"SERVER_NAME").decode("latin-1"),
                int(d_list_headers.get(b"SERVER_PORT").decode("latin-1"))
            ]
        }

        return scope
