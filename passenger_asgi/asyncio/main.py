import os
import sys
import json
import errno
import socket
import asyncio
import logging

from pathlib import Path
from abc import ABC, abstractmethod
from typing import Callable, Awaitable, NoReturn, Optional

from asyncio import IncompleteReadError
from asyncio import StreamReader, StreamWriter
from asyncio import set_event_loop_policy, get_event_loop
from asyncio import AbstractEventLoopPolicy, DefaultEventLoopPolicy, AbstractEventLoop, start_unix_server

from passenger_asgi.adapter import AdapterBase
from passenger_asgi.asyncio.lifespan import LifespanContainer
from passenger_asgi.asgi_typing import ASGI3App, Event, WSGIApp
from passenger_asgi.asgi_statemachine import StateMachine
from passenger_asgi.asyncio.websocket import WebSocketMiddleware
from passenger_asgi.asyncio.wsgi import WsgiContainer
from passenger_asgi.session import PassengerSessionProtocol
from passenger_asgi.passenger import Passenger, PassengerWorker


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

        await self.writer.drain()

        if data is None:
            self.writer = None

    async def start_response(self, event: Event):
        if event["type"] != "http.response.start":
            raise ValueError("You can't send this event here.")

        status = event["status"]
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

    def __init__(self, app, reader, writer):
        self.app = app

        self.protocol = PassengerSessionProtocol()
        self.reader: StreamReader = reader
        self.writer: StreamWriter = writer

    async def read_until_result(self):
        while True:
            if self.reader.at_eof():
                return self.protocol.feed(None)

            needed = self.protocol.state_requires_bytes()

            try:
                if needed == 0:
                    data = await self.reader.read(1024*1024)
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
            return await self.read_until_result()

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

    def wrap_wsgi(self, app: WSGIApp) -> ASGI3App:
        return WsgiContainer(app)

    @abstractmethod
    def get_event_loop_policy(self) -> AbstractEventLoopPolicy:
        pass

    @abstractmethod
    def prepare_async(self, app: ASGI3App) -> Awaitable[None]:
        pass

    def prepare(self, app: ASGI3App) -> NoReturn:
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
        self.app = app
        self.lifespan = LifespanContainer(app)
        await self.lifespan.startup()

    async def run_async(self) -> None:
        with self.passenger.run_state("SUBPROCESS_LISTEN"):
            config = self.wrapper.config
            base_path = Path(config.get("socket_dir"))

            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)

            for i in range(128):
                unique_id = int.from_bytes(os.urandom(8), "big").__format__("x")
                path = base_path / f"asgi.{unique_id}"
                try:
                    sock.bind(str(path))
                except OSError as e:
                    if e.errno == errno.EADDRINUSE:
                        continue
                    raise
                else:
                    break

            else:
                raise OSError(errno.EADDRINUSE, "Couldn't generate a server-socket.")

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
    def get_event_loop_policy(self) -> AbstractEventLoopPolicy:
        return DefaultEventLoopPolicy()


class UVLoopAdapter(SimpleAdapter):
    def get_event_loop_policy(self) -> AbstractEventLoopPolicy:
        import uvloop
        return uvloop.EventLoopPolicy()
