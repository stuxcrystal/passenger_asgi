#!/usr/bin/env python3


def loader():
    from passenger_asgi.passenger import PassengerWorker
    PassengerWorker.main()


def preloader():
    from passenger_asgi.preloader import PreloaderWorker
    PreloaderWorker.main()
