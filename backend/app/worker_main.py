from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Callable

from app.runtime import build_worker_runtime

logger = logging.getLogger(__name__)


RuntimeFactory = Callable[[], object]
SignalInstaller = Callable[[Callable[[], None]], None]


def _install_signal_handlers(request_stop: Callable[[], None]) -> None:
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, request_stop)
        except (NotImplementedError, RuntimeError):
            signal.signal(signum, lambda *_: loop.call_soon_threadsafe(request_stop))


async def _await_quietly(task: asyncio.Task[object]) -> None:
    try:
        await task
    except BaseException:
        pass


async def run_worker(
    *,
    runtime_factory: RuntimeFactory = build_worker_runtime,
    signal_installer: SignalInstaller = _install_signal_handlers,
) -> int:
    runtime = runtime_factory()
    stop = asyncio.Event()
    signal_installer(stop.set)
    worker_task = asyncio.create_task(runtime.service.run_forever(stop), name="worker-service")
    janitor_task = asyncio.create_task(runtime.janitor.run_forever(stop), name="snapshot-janitor")
    stop_task = asyncio.create_task(stop.wait(), name="worker-stop-signal")
    managed = {
        worker_task: "worker",
        janitor_task: "janitor",
    }
    try:
        done, _pending = await asyncio.wait(
            [*managed, stop_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        if stop_task in done:
            stop.set()
            await asyncio.gather(worker_task, janitor_task, return_exceptions=True)
            return 0

        failed_task = next(task for task in done if task in managed)
        component = managed[failed_task]
        logger.error("worker_runtime_task_stopped_unexpectedly component=%s", component)
        stop.set()
        for task in managed:
            if task is not failed_task and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in managed if task is not failed_task),
            return_exceptions=True,
        )
        await _await_quietly(failed_task)
        return 1
    finally:
        stop.set()
        if not stop_task.done():
            stop_task.cancel()
            await asyncio.gather(stop_task, return_exceptions=True)
        for task in managed:
            if not task.done():
                task.cancel()
        await asyncio.gather(worker_task, janitor_task, return_exceptions=True)
        await runtime.close()


def main() -> None:
    raise SystemExit(asyncio.run(run_worker()))


if __name__ == "__main__":
    main()
