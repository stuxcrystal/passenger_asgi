import asyncio
import sys
from asyncio import get_running_loop, CancelledError
from threading import Lock, Thread
from typing import TypeVar, Awaitable, NoReturn
from concurrent.futures import ThreadPoolExecutor, Future as ConcFuture

from passenger_asgi.asyncio.asgi_typing import WSGIApp, Headers, WriteCallable, Scope, Receive, Send

try:
    from _io import BytesIO
except ImportError:
    from io import BytesIO as RawBytesIO

    class BytesIO(object):
        def __init__(self, *args, **kwargs):
            self._lock = Lock()
            self._wrapped = RawBytesIO(*args, **kwargs)

        def __getattr__(self, item):
            with self._lock:
                result = getattr(self._wrapped, item)

            if callable(result):
                def _wrapper(*args, **kwargs):
                    with self._lock:
                        return result(*args, **kwargs)
                return _wrapper

            return result


T = TypeVar("T", covariant=True)


class WsgiContainer:

    def __init__(self, wsgi_application: WSGIApp):
        self.wsgi_application = wsgi_application
        self.executor = None

    def run_wsgi(self, env, send):
        def write(data: bytes) -> NoReturn:
            send({
                'type': 'http.response.body',
                'body': data,
                'more_body': True
            })

        def start_response(code: str, headers: Headers) -> WriteCallable:
            status_code, *_ = code.split()
            status_code = int(status_code)
            send({
                'type': 'http.response.start',
                'status': status_code,
                'headers': [(h.encode("ascii"), v.encode("ascii")) for h, v in headers]
            })
            return write

        for data in self.wsgi_application(env, start_response):
            write(data)

        send({
            'type': 'http.response.body',
            'body': b'',
            'more_body': False
        })

    async def run_wsgi_feeder(self, scope: Scope, receive: Receive, fut: Awaitable[None], input_io: BytesIO):
        while not fut.done():
            event = await receive()
            if event["type"] == "http.disconnect":
                input_io.close()
                break

            # Use exponential back-off until each attempt is 30 seconds apart.
            b = 0
            while len(input_io.getbuffer()) > 1024*1024:
                await asyncio.sleep(0.1 * (1.1**b))
                if b < 60:
                    b += 1

            input_io.write(event.get("body", b""))

            if not event.get("more_body", False):
                input_io.close()
                break

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] == "lifespan":
            if (await receive())["type"] == "lifespan.startup":
                self.executor = ThreadPoolExecutor(
                    max_workers=20,
                )
                await send({'type': 'lifespan.startup.complete'})

            if (await receive())["type"] == "lifespan.shutdown":
                fut = ConcFuture()
                def _shutdown():
                    try:
                        self.executor.shutdown()
                    finally:
                        fut.set_result(None)
                Thread(target=_shutdown).start()
                await asyncio.wrap_future(fut)
                await send({'type': 'lifespan.shutdown.complete'})

            return

        if scope["type"] != "http":
            return

        input_io = BytesIO()

        loop = get_running_loop()
        send_sync = lambda data: asyncio.run_coroutine_threadsafe(send(data), loop=loop).result()

        server = scope.get("server", ["localhost", 80])

        environ = {
            "REQUEST_METHOD": scope["method"],
            "SERVER_NAME": server[0],
            "SERVER_PORT": server[1],
            "SCRIPT_NAME": "",
            "PATH_INFO": scope["path"],
            "QUERY_STRING": scope["query_string"].decode("ascii"),
            "SERVER_PROTOCOL": "HTTP/%s" % scope["http_version"],
            "wsgi.version": (1, 0),
            "wsgi.url_scheme": scope.get("scheme", "http"),
            "wsgi.input": input_io,
            "wsgi.errors": sys.stderr,
            "wsgi.multithread": True,
            "wsgi.multiprocess": True,
            "wsgi.run_once": False,
        }

        fut = get_running_loop().run_in_executor(self.executor, lambda: self.run_wsgi(environ, send_sync))
        task = get_running_loop().create_task(self.run_wsgi_feeder(scope, receive, fut, input_io))
        await fut
        if not task.done():
            task.cancel()
            try:
                await task
            except CancelledError:
                pass
