from typing import Awaitable, Callable, MutableMapping, Union, Sequence, NoReturn, Tuple, Iterable, Any, Mapping

ScopeValue = Union[bytes, int, MutableMapping[str, 'ScopeValue'], Sequence]
Scope = MutableMapping[str, ScopeValue]
Event = MutableMapping[str, ScopeValue]

Receive = Callable[[], Awaitable[Event]]
Send = Callable[[Event], Awaitable[None]]

WriteCallable = Callable[[bytes], None]
Headers = Sequence[Tuple[str, str]]
StartResponse = Callable[[str, Headers], WriteCallable]
Environment = Mapping[str, Any]
WSGIApp = Callable[[Environment, StartResponse], Iterable[bytes]]

ASGI3App = Callable[[Scope, Receive, Send], NoReturn]

ASGI2Instance = Callable[[Receive, Send], NoReturn]
ASGI2App = Callable[[Scope], ASGI2Instance]