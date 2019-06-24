import abc
from typing import TYPE_CHECKING, NoReturn, Callable, Awaitable, Any

if TYPE_CHECKING:
    from passenger_asgi.passenger import Passenger, PassengerWorker


class AdapterBase(abc.ABC):

    def __init__(self, wrapper: 'PassengerWorker', passenger: 'Passenger'):
        self.wrapper = wrapper
        self.passenger = passenger

    @classmethod
    def get_type(cls) -> str:
        return "Generic Adapter"

    @abc.abstractmethod
    def prepare_application(self, app: Any) -> Any:
        pass

    @abc.abstractmethod
    def prepare(self, app: Any) -> NoReturn:
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
