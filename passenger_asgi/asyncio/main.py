import os
import sys
import json
import asyncio
import inspect
import logging

from pathlib import Path
from http.client import responses
from abc import ABC, abstractmethod
from typing import Callable, Awaitable, NoReturn, Optional, Any

from asyncio import IncompleteReadError
from asyncio import StreamReader, StreamWriter
from asyncio import set_event_loop_policy, get_event_loop
from asyncio import AbstractEventLoopPolicy, DefaultEventLoopPolicy, AbstractEventLoop, start_unix_server

from passenger_asgi.api import get_callbacks
from passenger_asgi.adapter import AdapterBase
from passenger_asgi.passenger import Passenger, PassengerWorker
from passenger_asgi.session import PassengerSessionProtocol, ProtocolState

from passenger_asgi.asyncio.wsgi import WsgiContainer
from passenger_asgi.asyncio.lifespan import LifespanContainer
from passenger_asgi.asyncio.exceptions import error_middleware
from passenger_asgi.asyncio.websocket import WebSocketMiddleware
from passenger_asgi.asyncio.asgi_statemachine import StateMachine
from passenger_asgi.asyncio.asgi_typing import ASGI3App, Event, Scope


class WriteStateMachine(StateMachine):

    def __init__(self, writer, head=False):
        self.writer = writer
        self.head = head
        super().__init__()

    def initial_state(self):
        return self.start_response

    async def try_write(self, data: Optional[bytes]):
        if self.writer is None:
            self.force_state(self.finished)
            await asyncio.sleep(0.1)
            return

        if data is None:
            self.writer.write_eof()
        else:
            self.writer.write(data)

        try:
            await self.writer.drain()
        except ConnectionResetError:
            self.writer = None
            return

        if data is None:
            self.writer = None

    async def start_response(self, event: Event):
        if event["type"] != "http.response.start":
            raise ValueError("You can't send this event here.")

        status = event["status"]
        status = f"{status} {responses[status]}"
        await self.try_write(f"HTTP/1.1 {status}\r\nStatus: {status}\r\nConnection: close\r\n".encode("latin-1"))
        for h, v in event.get("headers", []):
            await self.try_write(b''.join([h, b": ", v, b"\r\n"]))
        await self.try_write(b"\r\n")

        if self.head:
            await self.try_write(None)

        return self.send_body, None

    async def send_body(self, event: Event):
        if event["type"] != "http.response.body":
            raise ValueError("You can't send this event here.")

        if self.head:
            return (self.send_body if event.get("more_body", False) else self.finished), None

        next_state = self.send_body
        await self.try_write(event.get("body", b""))
        if not event.get("more_body", False):
            await self.try_write(None)
            next_state = self.finished

        return next_state, None

    async def finished(self, event: Event):
        raise IOError("The connection has been closed.")


class Connection:
    HTTPS_VALUES = (b"on", b"1", b"true", b"yes")

    def __init__(self, app, reader, writer):
        self.app = app

        self.protocol = PassengerSessionProtocol(self.parse_env)
        self.reader: StreamReader = reader
        self.writer: StreamWriter = writer

    def parse_env(self, list_headers):
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
            ],
        }

        if b'CONTENT_LENGTH' in d_list_headers:
            # We have an exact amount left.
            _left = int(d_list_headers[b'CONTENT_LENGTH'].decode("latin-1"))
            _state = ProtocolState.RECEIVING_DATA

        elif b'HTTP_TRANSFER_ENCODING' in d_list_headers or b'UPGRADE' in d_list_headers:
            _left = 0
            _state = ProtocolState.RECEIVING_DATA

        else:
            _left = 0
            _state = ProtocolState.CLOSED

        return _state, _left, scope

    async def read_until_result(self):
        print(self.protocol)
        if self.protocol.is_closed():
            return self.protocol.feed(None)

        while True:
            if self.reader.at_eof():
                return self.protocol.feed(None)

            needed = self.protocol.state_requires_bytes()

            try:
                if needed == 0:
                    data = await self.reader.read(1024*1024)
                elif isinstance(needed, bytes):
                    data = await self.reader.readuntil(b'\r\n')
                else:
                    data = await self.reader.readexactly(needed)

            except IncompleteReadError as e:
                return self.protocol.feed(e.partial)

            else:
                result = self.protocol.feed(data)

            if result is not None:
                return result

    async def run(self):
        scope = await self.read_until_result()

        if scope["method"] == "ping":
            self.writer.write(b"pong")
            self.writer.write_eof()
            await self.writer.drain()

        async def receive() -> Event:
            result = await self.read_until_result()
            return result

        send = WriteStateMachine(self.writer, scope["method"].lower() == "head")

        await self.app(scope, receive, send)
        await send.try_write(None)


class AsyncIOAdapter(AdapterBase, ABC):

    def __init__(self, worker: PassengerWorker, passenger: Passenger):
        self.logger = logging.getLogger("passenger_asgi.AsyncIOAdapter")

        self.event_loop: Optional[AbstractEventLoop] = None
        super().__init__(worker, passenger)

        set_event_loop_policy(self.get_event_loop_policy())
        self.event_loop = get_event_loop()

    def prepare_application(self, application: Any) -> Any:
        # Convert an WSGI-App to an ASGI-App if needed.
        signature = inspect.signature(application, follow_wrapped=True)
        if len(signature.parameters) == 2:
            application = WsgiContainer(application)

        # Convert an ASGI2-App to ASGI3
        from asgiref.compatibility import guarantee_single_callable
        application = guarantee_single_callable(application)

        return application

    @abstractmethod
    def get_event_loop_policy(self) -> AbstractEventLoopPolicy:
        pass

    @abstractmethod
    def prepare_async(self, app: ASGI3App) -> Awaitable[None]:
        pass

    def prepare(self, app: ASGI3App) -> NoReturn:
        for cb in get_callbacks("prepare-app"):
            cb()

        self.event_loop.run_until_complete(self.prepare_async(app))

    @abstractmethod
    def run_async(self) -> Awaitable[None]:
        pass

    def run(self) -> NoReturn:
        self.event_loop.run_until_complete(self.run_async())

    def register_signal(self, sig: int, callback: Callable[[int], Awaitable]) -> NoReturn:
        pass


class SimpleAdapter(AsyncIOAdapter, ABC):

    def detect_close(self):
        if not sys.stdin.read():
            self.event_loop.create_task(self.stop_graceful())
            self.event_loop.remove_reader(sys.stdin.fileno())

    async def prepare_async(self, app: ASGI3App) -> None:
        os.set_blocking(sys.stdin.fileno(), False)
        self.event_loop.add_reader(sys.stdin.fileno(), self.detect_close)

        ##
        # Prepare the application container.
        app = WebSocketMiddleware(app)
        app = error_middleware(app)
        self.app = app
        self.lifespan = LifespanContainer(app)
        await self.lifespan.startup()

    async def run_async(self) -> None:
        with self.passenger.run_state("SUBPROCESS_LISTEN"):
            config = self.wrapper.config
            base_path = Path(config.get("socket_dir"))
            sock, path = self.passenger.find_unix_socket(base_path)

            self.logger.debug(f"Publishing socket: {path}")
            with open(self.passenger.spawn_dir / "response" / "properties.json", "w") as f:
                json.dump({
                    "sockets": [{
                        "name": "main",
                        "address": f"unix:{path}",
                        "protocol": f"session",
                        "concurrency": 0,
                        "accept_http_requests": True
                    }]
                }, f)

        self.server = await start_unix_server(lambda r, w: Connection(self.app, r, w).run(), sock=sock)
        self.passenger.ready()
        self.event = asyncio.Event()
        await self.event.wait()

    async def stop_graceful(self) -> NoReturn:
        self.server.close()
        await self.server.wait_closed()
        await self.lifespan.shutdown()
        self.event.set()

    async def kill(self) -> NoReturn:
        self.event_loop.stop()
        self.event_loop.close()


class DefaultAdapter(SimpleAdapter):

    @classmethod
    def get_type(cls) -> str:
        return "ASGI-Worker"

    def get_event_loop_policy(self) -> AbstractEventLoopPolicy:
        return DefaultEventLoopPolicy()


class UVLoopAdapter(SimpleAdapter):
    @classmethod
    def get_type(cls) -> str:
        return "ASGI-Worker (uvloop)"

    def get_event_loop_policy(self) -> AbstractEventLoopPolicy:
        import uvloop
        return uvloop.EventLoopPolicy()
