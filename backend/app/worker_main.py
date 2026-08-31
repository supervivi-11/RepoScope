from __future__ import annotations

import asyncio
import signal

from app.runtime import build_worker_runtime


async def run_worker() -> None:
    runtime = build_worker_runtime()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop.set)
        except (NotImplementedError, RuntimeError):
            signal.signal(signum, lambda *_: loop.call_soon_threadsafe(stop.set))
    worker_task = asyncio.create_task(runtime.service.run_forever(stop))
    janitor_task = asyncio.create_task(runtime.janitor.run_forever(stop))
    try:
        await stop.wait()
    finally:
        stop.set()
        await asyncio.gather(worker_task, janitor_task, return_exceptions=True)
        await runtime.close()


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
