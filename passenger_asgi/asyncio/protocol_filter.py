def filter_protocols(app, supported=("http")):
    async def _middleware(scope, receive, send):
        if scope["type"] in supported:
            return await app(scope, receive, send)

        # Just ignore the lifespan protocol.
        if scope["type"] == "lifespan":
            return

        # Reject Websocket connections
        elif scope["type"] == "websocket":
            await send({'type': 'websocket.close', 'code': 400})

        # Return a 400 for http.
        elif scope["type"] == "http":
            message = b'This server does not support plain-http.'
            await send({'type': 'http.response.start', 'status': 400, 'headers': [
                (b'Content-Length', str(len(message)).encode('latin-1'))
            ]})
            await send({'type': 'http.response.body', 'body': message, 'more_body': False})

        else:
            raise ValueError("Unsupported protocol. Cannot return correct message.")

    return _middleware