from abc import ABC, abstractmethod
from asyncio import get_running_loop, CancelledError, shield


class StateMachine(ABC):

    def __init__(self):
        self._current_state = self.initial_state()
        self._current_task = None
        self._cancel_inner = False
        self.queue = []

    @abstractmethod
    def initial_state(self):
        pass

    def force_state(self, state):
        self._current_state = state
        if self._current_task is not None:
            self._current_task.cancel()

    async def _run(self, args, kwargs):
        while self._current_state is not None:
            self._current_task = get_running_loop().create_task(self._current_state(*args, *kwargs))
            try:
                next_state, result = await self._current_task
                self._current_state = next_state
                return result

            except CancelledError:
                if self._cancel_inner:
                    self._cancel_inner = False
                    return

                if self._current_state is not None:
                    continue

            finally:
                if self.queue:
                    self.queue[0].set_result()
                else:
                    self._current_task = None

    async def __call__(self, *args, **kwargs):
        if self._current_task is not None:
            future = get_running_loop().create_future()
            self.queue.append(future)
            await future
            self.queue.remove(future)

        try:
            return await shield(self._run(args, kwargs))
        except CancelledError:
            if self._current_task is not None:
                self._cancel_inner = True
                self._current_task.cancel()
