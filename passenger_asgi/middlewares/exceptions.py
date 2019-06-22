import logging
import traceback


def error_middleware(app):
    async def _middleware(scope, receive, send):
        try:
            await app(scope, receive, send)
        except Exception as e:
            logging.error(f"Got an unexpected error in ASGI-Application.")
            exception = ''.join(traceback.format_exception(type(e), e, e.__traceback__)).split("\n")
            for line in exception:
                logging.error(line)
    return _middleware