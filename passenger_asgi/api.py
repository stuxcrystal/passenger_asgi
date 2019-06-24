from typing import NoReturn, Callable, List

__am_i_passenger = False
__prepare_callbacks = {}


def _set_passenger():
    global __am_i_passenger
    __am_i_passenger = True


def is_passenger() -> bool:
    return __am_i_passenger


def register_callback(name: str, cb: Callable[[], NoReturn]) -> NoReturn:
    if not is_passenger():
        raise EnvironmentError("We are not running inside a passenger-worker.")

    __prepare_callbacks.setdefault(name, []).append(cb)


def get_callbacks(name: str) -> List[Callable[[], NoReturn]]:
    return __prepare_callbacks.get(name, [])
