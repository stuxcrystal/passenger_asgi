import os
import logging
from enum import IntEnum, auto
from typing import Optional, Union, Callable, Sequence, Tuple, Any

from passenger_asgi.asyncio.asgi_typing import Scope, Event


class ProtocolState(IntEnum):
    # GETTING WSGI-Environment
    REQUIRES_HEADER_LENGTH = auto()
    HEADERS_INCOMING = auto()

    # CONTENT_LENGTH
    RECEIVING_DATA = auto()

    # REQUEST FINISHED
    CLOSED = auto()


class PassengerSessionProtocol(object):

    def __init__(self, parser: Callable[[Sequence[Tuple[bytes, bytes]]], Tuple[ProtocolState, int, Any]]):
        self._parser = parser

        self.sid = int.from_bytes(os.urandom(8), "big").__format__("x")
        self.logger = logging.getLogger(f"passenger_asgi.session.{self.sid}")
        self._state = ProtocolState.REQUIRES_HEADER_LENGTH

        self._required = 4
        self._left = -1

    def __repr__(self):
        return f"<PassengerSessionProtocol at {self._state} missing {self._required!r}>"

    def state_requires_bytes(self) -> [int, bytes]:
        """
        :return: 0 - Send what you have to .feed; >0 - Send *exactly* that many bytes to .feed;
                 If it is a bytes-object, use read_until.
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
            return self._received_full_event(data)

        elif self._state == ProtocolState.RECEIVING_DATA:
            if self._left > 0:
                data = data[:self._left]
                self._left -= len(data)
                if self._left == 0:
                    self._state = ProtocolState.CLOSED

            return {'type': 'http.request', 'body': data, 'more_body': True}

        else:
            raise RuntimeError("Unknown state?!")

    def _received_full_event(self, data: bytes) -> Scope:
        list_headers = data.split(b"\0")
        list_headers = list(zip(list_headers[::2], list_headers[1::2]))

        new_state, left, result = self._parser(list_headers)
        self._state = new_state
        self._left = left

        return result
