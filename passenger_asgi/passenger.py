import os
import sys
import time
import json
import inspect
import logging
import pathlib
from typing import NoReturn, Callable, Awaitable
from contextlib import contextmanager

import setproctitle

from passenger_asgi.adapter import AdapterBase
from passenger_asgi.asgi_typing import ASGI3App, WSGIApp
from passenger_asgi.middlewares.exceptions import error_middleware
from passenger_asgi.middlewares.protocol_filter import filter_protocols


class Passenger(object):

    def __init__(self, spawn_dir: pathlib.Path):
        self.spawn_dir = spawn_dir
        self.logger = logging.getLogger("passenger_asgi.states")

    def _maybe_write(self, path: pathlib.Path, content: str) -> NoReturn:
        try:
            with open(path, "w") as f:
                f.write(content)

        except FileNotFoundError:
            self.logger.debug(f"Tried to write to non-existing file: '{path}'")

        except Exception as e:
            self.logger.warn(f"Failed to write file '{path}': ({e.__class__.__name__}){e}")

    def _set_state(self, name: str, state: str) -> NoReturn:
        self.logger.debug(f"Setting state of '{name}' to '{state}'")
        self._maybe_write(self.spawn_dir / "response" / "steps" / name.lower() / "state", state)

    def _set_starttime(self, name: str) -> NoReturn:
        self._maybe_write(self.spawn_dir / "response" / "steps" / name.lower() / "begin_time", str(time.time()))

    def _set_endtime(self, name: str) -> NoReturn:
        directory = self.spawn_dir / "response" / "steps" / name.lower()
        if not os.path.exists(directory / "begin_time") and not os.path.exists(directory / "begin_time_monotonic"):
            self._set_starttime(name)
        self._maybe_write(directory / "end_time", str(time.time()))

    def begin_state(self, name: str) -> NoReturn:
        self._set_state(name, "STEP_IN_PROGRESS")

    def finish_state(self, name: str) -> NoReturn:
        self._set_state(name, "STEP_PERFORMED")
        self._set_endtime(name)

    def fail_state(self, name: str) -> NoReturn:
        self._set_state(name, "STEP_ERRORED")
        self._set_endtime(name)

    def ready(self):
        with open(self.spawn_dir / "response" / "finish", "w") as f:
            f.write("1")
        self.logger.debug("Application initialization finished.")

    @contextmanager
    def run_state(self, name: str):
        self.begin_state(name)
        try:
            yield
        except:
            self.fail_state(name)
            raise
        else:
            self.finish_state(name)


class PassengerWorker(object):

    def __init__(self, passenger: Passenger):
        self.passenger = passenger
        self.config = {}

    def _init_logging(self):
        logging.basicConfig(
            level=logging.WARNING if os.getenv("PASSENGER_ASGI_DEBUG", "off")=="off" else logging.DEBUG,
            format="[ pid=%(process)d, time=%(asctime)s ]: (%(name)s) %(message)s"
        )

    def set_proc_title(self, type="ASGI-Worker"):
        fallback_title = self.config.get('app_root', os.getcwd())
        app_group_name = self.config.get('app_group_name', fallback_title)
        setproctitle.setproctitle(f"wsgi-loader.py (loader redirected to passenger_asgi; {type}: {app_group_name})")

    def get_adapter(self, name) -> AdapterBase:
        if name == "default":
            from passenger_asgi.asyncio import DefaultAdapter
            return DefaultAdapter(self, self.passenger)

        import pkg_resources
        for ep in pkg_resources.iter_entry_points("passenger_asgi_adapters", name):
            adapter = ep.load()
            break
        else:
            raise EnvironmentError("Cannot find adapter.")

        return adapter(self, self.passenger)

    def load_app(self, adapter: AdapterBase) -> ASGI3App:
        from importlib.machinery import SourceFileLoader

        startup_file = self.config.get("startup_file", None)

        # Autodetect filename.
        if startup_file is None:
            if os.path.exists("passenger_asgi.py"):
                startup_file = "passenger_asgi.py"
            elif os.path.exists("passenger_wsgi.py"):
                startup_file = "passenger_wsgi.py"
            else:
                raise ImportError("Failed to find the wsgi-application.")

        # Import the module.
        module = SourceFileLoader("passenger_wsgi", startup_file).load_module()
        application = module.application

        # Convert an WSGI-App to an ASGI-App if needed.
        signature = inspect.signature(application, follow_wrapped=True)
        if len(signature.parameters) == 2:
            self.set_proc_title("ASGI-Worker wrapping WSGI")
            application = adapter.wrap_wsgi(application)

        # Convert an ASGI2-App to ASGI3
        from asgiref.compatibility import guarantee_single_callable
        application = guarantee_single_callable(application)

        return application

    def run(self):
        self._init_logging()
        self.passenger.finish_state("SUBPROCESS_EXEC_WRAPPER")
        with self.passenger.run_state("SUBPROCESS_WRAPPER_PREPARATION"):
            with open(self.passenger.spawn_dir / "args.json") as f:
                self.config = json.load(f)

            self.set_proc_title()

            adapter = os.getenv("PASSENGER_ASGI_ADAPTER", "default")
            adapter = self.get_adapter(adapter)

        with self.passenger.run_state("SUBPROCESS_APP_LOAD_OR_EXEC"):
            app = self.load_app(adapter)
            app = error_middleware(app)
            adapter.prepare(app)

        adapter.run()

    @classmethod
    def main(cls):
        passenger = Passenger(pathlib.Path(os.getenv("PASSENGER_SPAWN_WORK_DIR")))
        worker = cls(passenger)
        worker.run()

