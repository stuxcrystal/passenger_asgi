from typing import Any, Tuple

from asyncio import sleep
from asyncio import get_running_loop

from passenger_asgi.asyncio.asgi_typing import Event
from passenger_asgi.asyncio.asgi_statemachine import StateMachine


class ReceiveStateMachine(StateMachine):

    def initial_state(self):
        return self.sleep

    async def sleep(self) -> Tuple[Any, Event]:
        while True:
            await sleep(3600*24)

    async def startup(self) -> Tuple[Any, Event]:
        return self.sleep, {"type": "lifespan.startup"}

    async def shutdown(self) -> Tuple[Any, Event]:
        return self.sleep, {"type": "lifespan.shutdown"}


class SendStateMachine(StateMachine):

    def __init__(self, receive: ReceiveStateMachine):
        self.receive = receive
        self.stopped = False
        self._current_request = None

        super().__init__()

    def initial_state(self):
        return self._none_allowed

    async def _none_allowed(self, event: Event) -> Tuple[Any, None]:
        raise RuntimeError("You can't send an event right now.")

    async def _startup(self, event: Event) -> Tuple[Any, None]:
        if event["type"] == "lifespan.startup.complete":
            self._current_request.set_result(None)
        elif event["type"] == "lifespan.startup.failed":
            message = event.get("message", "*Sender did not choose to pass a message.*")
            self._current_request.set_exception(RuntimeError(message))
        else:
            raise RuntimeError("You can't send this message here.")

        return self._none_allowed, None

    async def _shutdown(self, event: Event):
        if event["type"] == "lifespan.shutdown.complete":
            self._current_request.set_result(None)
        elif event["type"] == "lifespan.shutdown.failed":
            message = event.get("message", "*Sender did not choose to pass a message.*")
            self._current_request.set_exception(RuntimeError(message))
        else:
            raise RuntimeError("You can't send this message here.")

        return self._none_allowed, None

    async def startup(self):
        if self.stopped:
            return

        self._current_request = get_running_loop().create_future()
        self.force_state(self._startup)
        self.receive.force_state(self.receive.startup)
        return await self._current_request

    async def shutdown(self):
        if self.stopped:
            return

        self._current_request = get_running_loop().create_future()
        self.force_state(self._shutdown)
        self.receive.force_state(self.receive.shutdown)
        return await self._current_request

    def cancel(self):
        self.stopped = True
        if self._current_request is not None:
            if not self._current_request.done():
                self._current_request.set_result(None)


class LifespanContainer:

    def __init__(self, app):
        self.receive = ReceiveStateMachine()
        self.send = SendStateMachine(self.receive)
        self.app = app
        self._task = get_running_loop().create_task(self.app({'type': 'lifespan'}, self.receive, self.send))
        self._task.add_done_callback(lambda _: self.send.cancel())

    async def startup(self):
        await self.send.startup()

    async def shutdown(self):
        await self.send.shutdown()
