"""Foreground vmbackupd process entry point."""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys

from .bootstrap import compose
from .config import DEFAULT_CONFIG_PATH, ConfigError, load_config
from .version import __version__


async def serve(components, stop_event: asyncio.Event | None = None) -> None:
    stop = stop_event or asyncio.Event()
    loop = asyncio.get_running_loop()
    installed = []
    for name in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(name, stop.set)
            installed.append(name)
        except (NotImplementedError, RuntimeError):
            pass
    started = False
    try:
        components.runtime.start()
        started = True
        await components.api_server.start()
        await stop.wait()
    finally:
        await components.api_server.stop_accepting()
        if started:
            components.runtime.stop()
        components.repository.close()
        components.api_server.remove_socket()
        for name in installed:
            loop.remove_signal_handler(name)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="vmbackupd")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--check-config", action="store_true")
    parser.add_argument("--version", action="store_true")
    args = parser.parse_args(argv)
    if args.version:
        print(__version__)
        return 0
    try:
        config = load_config(args.config)
        if args.check_config:
            print("configuration OK")
            return 0
        asyncio.run(serve(compose(config)))
        return 0
    except (ConfigError, ValueError, RuntimeError) as exc:
        print(f"vmbackupd: {exc}", file=sys.stderr)
        return 2
