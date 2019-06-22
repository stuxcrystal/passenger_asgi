#!/usr/bin/env python3


def loader():
    from passenger_asgi.passenger import PassengerWorker
    PassengerWorker.main()
