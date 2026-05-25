"""Minimal HTTP retry helper for Phase 1.

Three retries with 1s/2s/4s backoff on 429/5xx or transient RequestException.
Anything else (incl. 4xx other than 429) propagates immediately.
"""

import time
from collections.abc import Callable

import requests

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_BACKOFFS = (1.0, 2.0, 4.0)


def with_retries(fn: Callable[[], requests.Response]) -> requests.Response:
    last_exc: Exception | None = None
    resp: requests.Response | None = None
    max_attempts = len(_BACKOFFS) + 1
    for attempt in range(max_attempts):
        try:
            resp = fn()
        except requests.RequestException as e:
            last_exc = e
            if attempt == max_attempts - 1:
                raise
            time.sleep(_BACKOFFS[attempt])
            continue
        if resp.status_code in RETRY_STATUSES and attempt < max_attempts - 1:
            time.sleep(_BACKOFFS[attempt])
            continue
        return resp
    # Defensive — if we exit the loop without a response (all attempts raised), re-raise.
    if last_exc is not None:
        raise last_exc
    assert resp is not None
    return resp
