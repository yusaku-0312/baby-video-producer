"""Provider-neutral retry boundary, awaiting confirmed model contracts."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from .state import PipelineError, Run


@dataclass(frozen=True)
class RemoteJob:
    status: str
    request_id: str


class QueueProvider(Protocol):
    def submit(self) -> str: ...
    def poll(self, request_id: str) -> RemoteJob: ...


def submit_once(run: Run, stage: str, provider: QueueProvider) -> int:
    """POST is never retried after an exception: it may have been accepted."""
    attempt = run.claim(stage)
    try:
        request_id = provider.submit()
        run.submitted(attempt, request_id)
    except Exception:
        run.fail(attempt, acceptance_unknown=True)
        raise PipelineError("Submission outcome is unknown; reconcile before retrying") from None
    return attempt


def poll_with_retry(provider: QueueProvider, request_id: str, *,
                    retries: int = 3, delay: Callable[[float], None]) -> RemoteJob:
    """Only reads are retried, with a bounded backoff; provider errors are hidden."""
    for count in range(retries + 1):
        try:
            result = provider.poll(request_id)
            if result.request_id != request_id:
                raise PipelineError("Provider returned an unexpected request ID")
            return result
        except (TimeoutError, ConnectionError):
            if count == retries:
                raise PipelineError("Polling temporarily unavailable; resume with the saved request ID") from None
            delay(min(2 ** count, 30))
    raise AssertionError("Unreachable")
