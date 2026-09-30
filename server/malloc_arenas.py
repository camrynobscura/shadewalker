"""Cap glibc's malloc arenas for the server process.

Why (#111): glibc gives each thread that allocates
its own malloc arena, up to 8 x cores, and an arena keeps freed memory for
reuse instead of returning it. FastAPI runs every sync endpoint on a
threadpool, so /route's per-request arrays end up spread over several
arenas, each holding its own leftover space. pipeline/config.py
(SERVER_MALLOC_ARENAS) carries the measurements.

The usual knob is the MALLOC_ARENA_MAX environment variable, but on the
box that would live in the systemd unit, and changing the unit needs sudo
-- the deploy user may only restart the service. mallopt(M_ARENA_MAX, n)
sets the same limit from inside the process and ships with a normal
deploy. It only governs arenas created after the call, so it must run
before the threadpool's threads first allocate: server/app.py calls it at
the very start of startup, before the graph loads or any request arrives.

Linux/glibc only. Everywhere else (the Mac in dev, a musl libc) it does
nothing and returns False; correctness never depends on it, only memory.
"""
import ctypes
import sys

# malloc.h: #define M_ARENA_MAX -8
_M_ARENA_MAX = -8


def limit_malloc_arenas(arenas: int) -> bool:
    """Ask glibc for at most `arenas` malloc arenas. True if glibc took it."""
    if not sys.platform.startswith("linux"):
        return False
    try:
        # CDLL(None) = the symbols already loaded into this process, which
        # include the C library's -- no library path to guess.
        mallopt = ctypes.CDLL(None).mallopt
    except (OSError, AttributeError):
        return False
    mallopt.argtypes = (ctypes.c_int, ctypes.c_int)
    mallopt.restype = ctypes.c_int
    return mallopt(_M_ARENA_MAX, arenas) == 1
