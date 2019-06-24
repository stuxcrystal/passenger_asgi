import json
import os
import socket
import sys
from pathlib import Path
from threading import Thread
from typing import Callable, Awaitable, NoReturn, Optional, Any, IO

import gc
import select

from passenger_asgi.adapter import AdapterBase
from passenger_asgi.asyncio.asgi_typing import ASGI3App
from passenger_asgi.passenger import PassengerWorker, Passenger


AdapterFactory = Callable[['PassengerWorker', Passenger], AdapterBase]


class PreloadedPassenger(Passenger):

    def ready(self):
        super().ready()
        self.finish_state("PRELOADER_FINISH")


class ReplacableIO:

    def __init__(self, io: IO):
        self._io = io

    def __getattr__(self, item):
        return getattr(self._io, item)

    def replace(self, new_io: IO):
        if not self._io.closed:
            self._io.close()
        self._io = new_io


class PreloaderAdapter(AdapterBase):

    def __init__(self, preloader: 'PreloaderWorker', adapter_factory: AdapterFactory):
        super().__init__(preloader, preloader.passenger)
        self.adapter_factory = adapter_factory
        self.adapter: Optional[AdapterBase] = None

        self._is_child = False
        self._app = None

    def prepare_application(self, app: Any) -> Any:
        _wrapped = None

        def _deferred_call(*args, **kwargs):
            nonlocal _wrapped
            if _wrapped is None:
                _wrapped = self.adapter.prepare_application(app)
            return _wrapped(*args, **kwargs)

        return _deferred_call

    def spawn(self):
        if self.adapter is None:
            self.adapter = self.adapter_factory(self.wrapper, self.passenger)

    def prepare(self, app: ASGI3App) -> NoReturn:
        self._app = app

    def create_and_advertise_server(self):
        base_path = Path(self.wrapper.config.get("socket_dir"))

        sock, path = self.passenger.find_unix_socket(base_path)
        sock.listen(10)

        with open(self.passenger.spawn_dir / "response" / "properties.json", "w") as f:
            json.dump({
                "sockets": [{
                    "name": "main",
                    "address": f"unix:{path}",
                    "protocol": "preloader",
                    "concurrency": 1
                }]
            }, f)

        return sock, path

    def make_new_worker(self, command, client):
        sub_passenger = PreloadedPassenger(Path(command['work_dir']))
        sub_passenger.finish_state('PRELOADER_PREPARATION')
        sub_passenger.begin_state('PRELOADER_FORK_SUBPROCESS')

        try:
            pid = os.fork()
        except Exception:
            sub_passenger.fail_state('PRELOADER_FORK_SUBPROCESS')
            raise

        if pid == 0:
            self._is_child = True
            self._child_passenger = sub_passenger
            self._child_passenger.command = command
            os.environ["PASSENGER_SPAWN_WORK_DIR"] = command["work_dir"]

            with sub_passenger.run_state('PRELOADER_SEND_RESPONSE'):
                client.sendall(json.dumps({'result': 'ok', 'pid': os.getpid()}, ensure_ascii=True).encode("ascii") + b"\r\n")

            # Reopen stdin and stderr.
            if os.path.exists(self._child_passenger.spawn_dir / 'stdin'):
                sys.stdin.replace(open(self._child_passenger.spawn_dir / 'stdin', 'r'))
            if os.path.exists(self._child_passenger.spawn_dir / 'stdout_and_err'):
                fobj = open(self._child_passenger.spawn_dir / 'stdout_and_err', "w")
                sys.stderr.replace(fobj)
                sys.stdout.replace(fobj)

            os.set_blocking(sys.stdin.fileno(), True)
            os.set_blocking(sys.stdout.fileno(), True)

            sub_passenger.finish_state('PRELOADER_FORK_SUBPROCESS')
        else:
            Thread(target=lambda: os.waitpid(pid, 0)).start()

        client.close()

    def pass_to_main(self):
        self.adapter = self.adapter_factory(PreloadedWorker(self._child_passenger), self._child_passenger)
        with open(self._child_passenger.spawn_dir / 'args.json') as f:
            self.adapter.wrapper.config = json.load(f)

        with self._child_passenger.run_state('SUBPROCESS_PREPARE_AFTER_FORKING_FROM_PRELOADER'):
            self.adapter.wrapper.set_proc_title(self.adapter.get_type())
            self.adapter.prepare(self._app)

        self.adapter.run()

    def run(self) -> NoReturn:
        with self.passenger.run_state("SUBPROCESS_LISTEN"):
            sock, path = self.create_and_advertise_server()
            self.server = sock

        gc.collect()
        gc.freeze()

        self.passenger.ready()
        while not self._is_child:
            r, _, _ = select.select([sys.stdin, sock], [], [])
            if sys.stdin in r:
                break

            client, _ = sock.accept()
            reader = client.makefile('r')
            data = reader.readline()
            command = json.loads(data, encoding='utf-8')
            if command['command'] == 'spawn':
                self.make_new_worker(command, client)
            else:
                name = command['command']
                client.sendall(json.dumps({
                    'result': 'error',
                    'message': f"Unknown command {name}"
                }, ensure_ascii=True))

        if not self._is_child:
            sock.shutdown(socket.SHUT_RDWR)
        sock.close()

        if self._is_child:
            self.pass_to_main()

    def stop_graceful(self) -> NoReturn:
        if self.adapter is not None:
            self.adapter.stop_graceful()
        else:
            self.server.shutdown(socket.SHUT_RDWR)

    def kill(self) -> NoReturn:
        if self.adapter is not None:
            self.adapter.kill()
        else:
            self.server.shutdown(socket.SHUT_RDWR)

    def register_signal(self, sig: int, callback: Callable[[int], Awaitable]) -> NoReturn:
        pass


class PreloadedWorker(PassengerWorker):
    def set_proc_title(self, type="ASGI-Worker"):
        super().set_proc_title(type)


class PreloaderWorker(PassengerWorker):

    def set_proc_title(self, type="ASGI-Worker"):
        super().set_proc_title(f"{type}")

    def get_adapter(self, name) -> AdapterFactory:
        factory = lambda p, _: PreloaderAdapter(p, super().get_adapter(name))
        factory.get_type = lambda: f"Preloader for {super(PreloaderWorker, self).get_adapter(name).get_type()}"
        return factory

    def run(self):
        sys.stdin = ReplacableIO(sys.stdin)
        sys.stderr = ReplacableIO(sys.stderr)
        sys.stdout = ReplacableIO(sys.stdout)

        super().run()