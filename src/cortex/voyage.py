"""Every request to Voyage goes through here: key, timeout, the one retry, and what gets measured.

The embedder and the reranker only shape their request bodies and read their replies. Failures
are raised to them unchanged (httpx errors), since each caller decides how to do without Voyage.
"""

from __future__ import annotations

import time

import httpx

from . import telemetry

API = "https://api.voyageai.com/v1"
# A rate limit or a server hiccup is worth one retry; anything else is the caller's problem.
RETRYABLE = (429, 500, 502, 503, 504)
RETRY_PAUSE_S = 1.0


class Voyage:
    def __init__(self, key: str, client: httpx.Client | None = None):
        self._key = key
        self._client = client or httpx.Client()

    def post(self, url: str, body: dict, *, operation: str, items: int, timeout: float, retry: bool) -> dict:
        """The reply's JSON. `operation` and `items` (texts or documents sent) label the telemetry."""
        with telemetry.span(f"voyage.{operation}", {"voyage.items": items, "voyage.retry": retry}) as span:
            for attempt in range(2):
                started = time.perf_counter()
                try:
                    response = self._client.post(
                        url, headers={"Authorization": f"Bearer {self._key}"}, json=body, timeout=timeout
                    )
                except httpx.TimeoutException:
                    telemetry.voyage_call(operation, items, time.perf_counter() - started, "timeout")
                    raise
                except httpx.HTTPError:
                    telemetry.voyage_call(operation, items, time.perf_counter() - started, "unreachable")
                    raise
                status = response.status_code
                outcome = "ok" if status < 400 else str(status)
                telemetry.voyage_call(operation, items, time.perf_counter() - started, outcome)
                span.set_attribute("http.response.status_code", status)
                if retry and status in RETRYABLE and attempt == 0:
                    time.sleep(RETRY_PAUSE_S)
                    continue
                response.raise_for_status()
                return response.json()
        raise AssertionError("unreachable")
