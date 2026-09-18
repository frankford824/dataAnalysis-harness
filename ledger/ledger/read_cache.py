"""Bounded read caches: single flight per key, no lock across computation."""
from concurrent.futures import Future
from threading import RLock

_guard = RLock()
_flights = {}


def cached(cache, key, build, maximum):
    identity = (id(cache), key)
    with _guard:
        if key in cache:
            cache.move_to_end(key)
            return cache[key]
        future = _flights.get(identity)
        owner = future is None
        if owner:
            future = _flights[identity] = Future()
    if not owner:
        return future.result()
    try:
        value = build()
        with _guard:
            cache[key] = value
            cache.move_to_end(key)
            while len(cache) > maximum:
                cache.popitem(last=False)
        future.set_result(value)
        return value
    except BaseException as exc:
        future.set_exception(exc)
        raise
    finally:
        with _guard:
            _flights.pop(identity, None)
