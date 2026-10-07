"""Coordinate Docker development tests and formal Runs across runtime workers."""
from contextlib import contextmanager
from pathlib import Path
import time


class GPUResourceBusy(RuntimeError):
    pass


@contextmanager
def gpu_slot(runtime_root: Path, *, wait=False, check_cancelled=None, occupied=None):
    import fcntl

    runtime_root.mkdir(parents=True, exist_ok=True)
    with (runtime_root / "gpu-resource.lock").open("a") as handle:
        while True:
            if check_cancelled:
                check_cancelled()
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                # A killed worker can leave a Docker process behind. Its durable
                # owner label prevents handing out the GPU after flock closes.
                if occupied is not None and occupied():
                    fcntl.flock(handle, fcntl.LOCK_UN)
                    if not wait:
                        raise GPUResourceBusy("A managed GPU container still owns the device; inspect its original receipt")
                    time.sleep(0.5)
                    continue
                break
            except BlockingIOError:
                if not wait:
                    raise GPUResourceBusy("GPU is reserved by another development test or formal Run")
                time.sleep(0.5)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
