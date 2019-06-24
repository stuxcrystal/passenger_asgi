import json
import logging
import os
import socket
import sys
import traceback
from pathlib import Path
from typing import Callable, Awaitable, NoReturn, Tuple, Sequence, cast, Any

import select

from passenger_asgi.adapter import AdapterBase
from passenger_asgi.api import get_callbacks
from passenger_asgi.asyncio.asgi_typing import ASGI3App, WSGIApp
from passenger_asgi.session import PassengerSessionProtocol, ProtocolState
from passenger_asgi.wsgi.restricted_input import RestrictedInput


class WSGIAdapter(AdapterBase):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.logger = logging.getLogger("passenger_asgi.WSGIAdapter")

    @classmethod
    def get_type(cls) -> str:
        return "WSGI-Worker"

    def prepare_application(self, app: Any) -> Any:
        return app

    def prepare(self, app: ASGI3App) -> NoReturn:
        self._app = cast(WSGIApp, app)

        for cb in get_callbacks("prepare-app"):
            cb()

    def read_until(self, sock: socket.socket, session: PassengerSessionProtocol):
        while not session.is_closed():
            data = bytearray()

            c = session.state_requires_bytes()
            while c > 0:
                read = sock.recv(c)
                if not read:
                    return None
                data += read
                c -= len(read)

            result = session.feed(bytes(data))
            if result is not None:
                return result

    def make_env(self, raw_env: Sequence[Tuple[bytes, bytes]]) -> Tuple[ProtocolState, int, dict]:
        env = {k.decode("latin-1"): v.decode("latin-1") for k, v in raw_env}

        env['wsgi.errors'] = sys.stderr
        env['wsgi.version'] = (1, 0)
        env['wsgi.multithread'] = False
        env['wsgi.multiprocess'] = True
        env['wsgi.run_once'] = False
        if env.get('HTTPS', 'off') in ('on', '1', 'true', 'yes'):
            env['wsgi.url_scheme'] = 'https'
        else:
            env['wsgi.url_scheme'] = 'http'

        return ProtocolState.CLOSED, 0, env

    def begin_request(self, env, client: socket.socket):
        input = client.makefile('r')

        if 'CONTENT_LENGTH' in env:
            input = RestrictedInput(input, int(env["CONTENT_LENGTH"]))
        elif 'HTTP_TRANSFER_ENCODING' not in env:
            # Set it to eof automatically.
            input = RestrictedInput(input, 0)

        env['wsgi.input'] = input

        is_head = env['REQUEST_METHOD'] == 'HEAD'

        response = None
        response_sent = False

        def write(data: bytes):
            nonlocal response_sent, response
            if not response_sent:
                response_sent = True
                if response is None:
                    response = ["200 Ok", []]

                client.sendall(b'HTTP/1.1 %s\r\nStatus: %s\r\nConnection: close\r\n' % (response[0].encode("ascii"), response[0].encode("ascii")))
                for header in response[1]:
                    client.sendall(b'%s: %s\r\n' % (header[0].encode("latin-1"), header[1].encode("latin-1")))
                client.sendall(b'\r\n')

                self.logger.debug("Response sent")

            if not is_head:
                client.sendall(data)

        def start_response(status, headers):
            nonlocal response
            self.logger.debug(f"Receiving response: {status}")

            response = [status, headers]
            return write

        result = None
        try:
            self.logger.debug(repr(env))
            self.logger.debug(self._app)
            result = self._app(env, start_response)
            for msg in result:
                write(msg)

            if not response_sent:
                write(b"")
        except Exception as e:
            print("Got error in WSGI Callback.")
            for line in '\n'.join(traceback.format_exception(type(e), e, e.__traceback__)).split("\n"):
                print(line, sep="", end="\n")

        finally:
            if hasattr(result, 'close'):
                result.close()

        client.shutdown(socket.SHUT_WR)
        client.close()

    def run(self) -> NoReturn:
        with self.passenger.run_state("SUBPROCESS_LISTEN"):
            base_path = Path(self.wrapper.config.get("socket_dir"))

            sock, path = self.passenger.find_unix_socket(base_path)

            self.logger.debug(str(os.listdir(self.passenger.spawn_dir / "response")))
            with open(self.passenger.spawn_dir / "response" / "properties.json", "w") as f:
                json.dump({
                    "sockets": [{
                        "name": "main",
                        "address": f"unix:{path}",
                        "protocol": "session",
                        "concurrency": 1,
                        "accept_http_requests": True
                    }]
                }, f)

        sock.listen(100)
        self.passenger.ready()
        while True:
            r, _, _ = select.select([sys.stdin, sock], [], [])
            if sys.stdin in r:
                break

            client, address = sock.accept()
            session = PassengerSessionProtocol(self.make_env)

            env = self.read_until(client, session)
            if env["REQUEST_METHOD"] == "PING":
                client.sendall(b"pong")
                client.close()
                continue

            self.begin_request(env, client)

        sock.close()

    def stop_graceful(self) -> NoReturn:
        pass

    def kill(self) -> NoReturn:
        pass

    def register_signal(self, sig: int, callback: Callable[[int], Awaitable]) -> NoReturn:
        pass