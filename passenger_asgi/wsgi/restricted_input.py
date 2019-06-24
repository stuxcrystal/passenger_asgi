import io
from threading import Lock
from typing import IO, Any, Callable, Optional


def _inherit(name: str) -> Callable[..., Any]:
    def _impl(self, *args, **kwargs):
        return getattr(self._parent, name)(*args, **kwargs)
    return _impl


class RestrictedInput(io.RawIOBase):
    """
    Acts like a socket input.

    Ignore what we know doesn't work.
    """

    def __init__(self, parent: IO, size: int):
        self._parent = parent
        self._lock = Lock()
        self._left = size

    close = _inherit("close")

    def seekable(self) -> bool:
        return False

    def readable(self) -> bool:
        return True

    def writable(self) -> bool:
        return False

    fileno = _inherit("fileno")

    def readinto(self, b: bytearray) -> Optional[int]:
        # Most likely b is empty or we're done.
        # Since in no case the internal state will change further, don't engage the lock.
        if self._left == 0 or len(b) == 0:
            return 0

        with self._lock:
            to_read = self._left if len(b) > self._left else len(b)

            view = b[:to_read]
            result = self._parent.readinto(view)
            if result is None:
                return None

            self._left -= result

        return result
