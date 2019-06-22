import traceback
from typing import Awaitable, Callable, List
from typing import Iterable, NoReturn, Optional, Union

from asyncio import Task, sleep, wait
from asyncio import Queue, Event as AsyncIOEvent, get_running_loop

import h11
from h11 import Request as H11Request
from wsproto import WSConnection
from wsproto.connection import SERVER, ConnectionState
from wsproto.events import Request, TextMessage, Message, BytesMessage, CloseConnection, AcceptConnection, Event


from passenger_asgi.asgi_typing import Scope, Receive, Send, ASGI3App

PullResult = Union[list, Event]


def _report_error(future):
    if future.exception() is not None:
        e = future.exception()
        traceback.print_exc(type(e), e, e.__traceback__)


def fire_and_almost_forget(coro):
    get_running_loop().create_task(coro).add_done_callback(_report_error)


class QueuePort:

    PORT_CLOSED_LEFTOVER = []
    PORT_CLOSED_SENTINEL = []

    def __init__(self):
        self.closed: Optional[Event] = None
        self._closed_event = AsyncIOEvent()

        self.receive_queue = Queue(maxsize=1)
        self.send_queue = Queue(maxsize=1)
        self._senders = 0
        self._consumers = 0

    async def send(self, event: Event) -> NoReturn:
        if event is None:
            raise RuntimeError("Event cannot be NULL")

        if self.closed is not None:
            return
        await self.send_queue.put(event)

    async def receive(self) -> Event:
        if self.closed is not None:
            return self.closed

        self._consumers += 1
        try:
            return await self.receive_queue.get()
        finally:
            self._consumers -= 1
            if self._consumers == 0 and self.closed is not None and not self._closed_event.is_set():
                self._closed_event.set()

    async def close(self, event: Event):
        self.closed = event

        while self._consumers > 0:
            await self.receive_queue.put(event)

        while self._senders > 0:
            await self.send_queue.put(None)

    async def pull(self) -> PullResult:
        self._senders += 1

        try:
            if self.send_queue.empty() and self.closed is not None:
                return self.PORT_CLOSED_SENTINEL

            value = await self.send_queue.get()
            if self.closed is not None and value is None:
                return self.PORT_CLOSED_SENTINEL

            return value
        finally:
            self._senders -= 1

    async def put(self, event: Event):
        if self.closed is None:
            await self.receive_queue.put(event)


def scope_to_request(scope: Scope) -> H11Request:
    raw_target = scope["path"].encode("utf-8")
    if scope.get("query_string", "") != "":
        raw_target += b"?" + scope["query_string"]

    return H11Request(
        method=scope["method"],
        target=raw_target,
        headers=scope["headers"]
    )


def get_header_from_scope(name: bytes, scope: Scope, *, split_comma=False) -> Iterable[bytes]:
    name = name.lower()

    for header, value in scope["headers"]:
        if header.lower() != name:
            continue

        if split_comma:
            for subvalue in value.split(b","):
                yield subvalue.strip()
        else:
            yield value


class KeepaliveManager:

    def __init__(self):
        self.current_task: Optional[Task] = None
        self.subscribers: List[Callable[[], Awaitable[None]]] = []

    async def cycle(self):
        while True:
            await sleep(30)

            # No point hogging resources if there is no subscriber left.
            if not self.subscribers:
                self.current_task = None
                break

            await wait([cb() for cb in self.subscribers])

    def subscribe(self, subscriber: Callable[[], Awaitable[None]]) -> None:
        self.subscribers.append(subscriber)
        if self.current_task is None:
            self.current_task = get_running_loop().create_task(self.cycle())

    def unsubscribe(self, subscriber: Callable[[], Awaitable[None]]):
        self.subscribers.remove(subscriber)


class WebSocketMiddleware:

    KEY_UUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    def __init__(self, app: ASGI3App):
        self.app = app
        self.keepalive = KeepaliveManager()

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        # A WebSocket-Request must be a GET request.
        if scope["method"].lower() != "get":
            return await self.app(scope, receive, send)

        # We did not detect an upgrade header.
        upgrade = tuple(get_header_from_scope(b"upgrade", scope))
        if not upgrade or upgrade[0].lower() != b"websocket":
            return await self.app(scope, receive, send)

        request = scope_to_request(scope)
        websocket = WSConnection(SERVER)
        connection = h11.Connection(h11.CLIENT)

        # Receive the actual connection.
        websocket.receive_data(connection.send(request))

        event: Request = next(websocket.events())
        assert isinstance(event, Request)
        headers = event.extra_headers
        host = event.host
        headers.append([b"host", host.encode("utf-8")])
        subprotocols = event.subprotocols

        extensions = scope.get("extensions", {}).copy()
        extensions["websocket.http.response"] = {}

        ws_scope = {
            "type":         "websocket",
            ###############################################################################
            "path":         scope["path"],
            "headers":      headers,
            "subprotocols": subprotocols,
            ###############################################################################
            "asgi":         scope.get("asgi", {"version": "3.0", "spec_version": "2.1"}),
            "http_version": scope.get("http_version", "1.1"),
            "scheme":       "ws" + (scope.get("scheme", "http").endswith("s") * "s"),
            "query_string": scope.get("query_string", b""),
            "root_path":    scope.get("root_path", b""),
            "client":       scope.get("client", None),
            "server":       scope.get("server", None),
            "extensions":   extensions
        }

        async def finish_events(port: QueuePort):
            buffer = []
            for event in websocket.events():
                if isinstance(event, CloseConnection):
                    await port.close({"type": "websocket.disconnect", "code": event.code})
                    return

                elif isinstance(event, Message):
                    buffer += event.data
                    if event.message_finished:
                        base = '' if isinstance(event, TextMessage) else b''
                        key = 'text' if isinstance(event, TextMessage) else 'bytes'
                        data = base.join(buffer)
                        buffer = []
                        await port.put({"type": "websocket.receive", key: data})

        async def receiver(receive: Receive, port: QueuePort):
            while websocket.state not in (ConnectionState.CLOSED, ConnectionState.REMOTE_CLOSING):
                event = await receive()
                websocket.receive_data(event.get("body", b""))

                await finish_events(port)

                if not event.get("more_body", False):
                    websocket.receive_data(None)
                    await finish_events(port)
                    return

        def instrument_errors(func):
            async def _wrapper(*args, **kwargs):
                try:
                    return await func(*args, **kwargs)
                except BaseException as e:
                    import traceback
                    traceback.print_exception(type(e), e, e.__traceback__)
            return _wrapper

        async def sender(send: Send, port: QueuePort):
            state = 'connecting'

            while websocket.state not in (ConnectionState.CLOSED, ConnectionState.LOCAL_CLOSING):
                event = await port.pull()
                if event is QueuePort.PORT_CLOSED_SENTINEL: return

                assert isinstance(event, dict)

                if event["type"] == "websocket.send":
                    if "bytes" in event:
                        await send({
                            'type': 'http.response.body',
                            'body': websocket.send(BytesMessage(data=event["bytes"])),
                            'more_body': True
                        })
                    elif "text" in event:
                        await send({
                            'type': 'http.response.body',
                            'body': websocket.send(TextMessage(data=event["text"])),
                            'more_body': True
                        })

                elif event["type"] == "websocket.close":
                    if state == "connected":
                        code = event.get("code", 1000)
                        await send({
                            'type': 'http.response.body',
                            'body': websocket.send(CloseConnection(code=code)),
                            'more_body': False
                        })
                        await port.close({'type': 'websocket.close', 'code': code})
                    else:
                        state = "denied"
                        await send({
                            'type': 'http.response.start',
                            'status': 403,
                            'headers': []
                        })
                        await send({'type': 'http.response.body', 'body': b'', 'more_body': False})
                        await port.close({'type': 'websocket.close'})

                elif event["type"] in ("websocket.http.response.start", "websocket.http.response.body"):
                    if state == "connected":
                        raise ValueError("You already accepted a websocket connection.")
                    if state == "connecting" and event["type"] == "websocket.http.response.body":
                        raise ValueError("You did not start a response.")
                    elif state == "denied" and event["type"] == "websocket.http.response.start":
                        raise ValueError("You already started a response.")

                    state = "denied"
                    event = event.copy()
                    event["type"] = event["type"][len("websocket."):]
                    await send(event)

                elif event["type"] == "websocket.accept":
                    raw = websocket.send(AcceptConnection(
                        extra_headers=event.get("headers", []),
                        subprotocol=event.get("subprotocol", None))
                    )
                    connection.receive_data(raw)
                    response = connection.next_event()
                    state = "connected"
                    await send({
                        'type': 'http.response.start',
                        'status': response.status_code,
                        'headers': response.headers
                    })

        port = QueuePort()
        await port.put({'type': 'websocket.connect'})

        loop = get_running_loop()
        receive_task = loop.create_task(instrument_errors(receiver)(receive, port))
        send_task = loop.create_task(instrument_errors(sender)(send, port))

        try:
            await self.app(ws_scope, port.receive, port.send)
        finally:
            await send_task
            await receive_task
