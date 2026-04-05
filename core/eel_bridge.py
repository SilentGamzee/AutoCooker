"""
eel_bridge — thread-safe bridge for calling eel.* from OS threads.

PROBLEM
-------
eel's WebSocket uses gevent sockets, which are NOT safe to call from real
OS threads (e.g. threading.Thread). On Windows with Python 3.12, gevent
does NOT monkey-patch threading.Thread, so the pipeline runs as a genuine
OS thread.

Calling gevent.spawn(fn) from an OS thread creates a greenlet on a
*per-thread hub* (a new, isolated hub created by gevent.get_hub() when
called from a non-gevent thread). That per-thread hub's event loop is not
running, so the greenlet never executes and the eel call is silently lost.

SOLUTION
--------
Use the only truly thread-safe gevent primitive: the async_ watcher.
watcher.send() is documented to be callable from any OS thread and safely
wakes up the *main* hub's event loop, which then drains a shared queue of
pending eel callbacks.

USAGE
-----
1. Call eel_bridge.setup() from the main thread, BEFORE eel.start().
2. From any thread: eel_bridge.call(lambda: eel.some_function(args))

Both main.py's _gevent_safe and base.py's BasePhase._gevent_safe use this.
"""
from __future__ import annotations

import collections
import threading

# Thread-safe deque — append/popleft are atomic in CPython
_queue: collections.deque = collections.deque()

# The async_ watcher — created on the main hub by setup()
_watcher = None
_watcher_lock = threading.Lock()


def setup() -> None:
    """
    Initialise the bridge.  MUST be called from the main thread before
    eel.start() so the watcher is attached to the correct (main) hub.
    """
    global _watcher
    try:
        import gevent
        hub = gevent.get_hub()
        w = hub.loop.async_()

        def _drain(watcher, revents):
            """Called by the main hub when watcher.send() wakes it up."""
            while _queue:
                try:
                    fn = _queue.popleft()
                    fn()
                except IndexError:
                    break
                except Exception as exc:
                    print(
                        f"[EEL_BRIDGE] callback raised "
                        f"{type(exc).__name__}: {exc}",
                        flush=True,
                    )

        w.start(_drain)
        with _watcher_lock:
            _watcher = w
        print("[EEL_BRIDGE] Initialized — async_ watcher attached to main hub", flush=True)

    except Exception as exc:
        print(f"[EEL_BRIDGE] setup() failed: {type(exc).__name__}: {exc}", flush=True)


def call(fn) -> None:
    """
    Schedule fn() to run in the main gevent event loop.
    Thread-safe: safe to call from any OS thread, gevent threadpool worker,
    or greenlet.

    If the bridge was not initialised (setup() not called), falls back to
    gevent.spawn() so behaviour degrades gracefully in test environments.
    """
    _queue.append(fn)

    with _watcher_lock:
        w = _watcher

    if w is not None:
        try:
            w.send()  # Thread-safe: wakes up the main hub's event loop
        except Exception as exc:
            print(f"[EEL_BRIDGE] watcher.send() failed: {exc}", flush=True)
    else:
        # Bridge not initialised — fall back to gevent.spawn (works from
        # greenlet context; silently no-ops from a bare OS thread)
        try:
            import gevent as _g
            _g.spawn(fn)
        except Exception:
            try:
                fn()          # last resort: direct call
            except Exception:
                pass
