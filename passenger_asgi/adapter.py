import abc
from typing import TYPE_CHECKING, NoReturn, Callable, Awaitable

from passenger_asgi.asgi_typing import ASGI3App, WSGIApp

if TYPE_CHECKING:
    from passenger_asgi.passenger import Passenger, PassengerWorker


class AdapterBase(abc.ABC):

    def __init__(self, wrapper: 'PassengerWorker', passenger: 'Passenger'):
        self.wrapper = wrapper
        self.passenger = passenger

    @abc.abstractmethod
    def wrap_wsgi(self, app: WSGIApp) -> ASGI3App:
        pass

    @abc.abstractmethod
    def prepare(self, app: ASGI3App) -> NoReturn:
        pass

    @abc.abstractmethod
    def run(self) -> NoReturn:
        pass

    @abc.abstractmethod
    def stop_graceful(self) -> NoReturn:
        pass

    @abc.abstractmethod
    def kill(self) -> NoReturn:
        pass

    @abc.abstractmethod
    def register_signal(self, sig: int, callback: Callable[[int], Awaitable]) -> NoReturn:
        pass
