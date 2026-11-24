import asyncio
from contextlib import suppress


def deliver_stream_error(queue: asyncio.Queue, error: Exception) -> None:
    while True:
        try:
            queue.put_nowait(error)
            return
        except asyncio.QueueFull:
            with suppress(asyncio.QueueEmpty):
                queue.get_nowait()
