from distutils.core import setup

from setuptools import find_packages

setup(
    name='passenger-asgi',
    version='0.1.0',
    packages=find_packages(),
    url='myurl',
    license='MIT',
    author='stuxcrystal',
    author_email='stuxcrystal@encode.moe',
    description='Enable ASGI support on a Passenger-Server',

    install_requires=["asgiref", "wsproto", "h11", "setproctitle"],
    extras_require={
        "uvloop": ["uvloop"]
    },

    entry_points={
        "console_scripts": [
            'passenger-asgi=passenger_asgi.entrypoint:loader',
            'passenger-asgi-preloader=passenger_asgi.entrypoint:preloader'
        ],
        "passenger_asgi_adapters": [
            "asyncio=passenger_asgi.asyncio:DefaultAdapter",
            "uvloop=passenger_asgi.asyncio:UVLoopAdapter",
            "wsgi=passenger_asgi.wsgi:WSGIAdapter"
        ]
    }
)
