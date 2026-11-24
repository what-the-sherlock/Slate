import random

import pytest
from hypothesis import given
from hypothesis import strategies as st

from core.domain.jobs import (
    BASE_BACKOFF,
    MAX_BACKOFF,
    MIN_BACKOFF,
    JobStatus,
    next_status_after_failure,
    retry_delay,
)


@given(st.integers(min_value=1, max_value=200), st.integers(min_value=0, max_value=2**32))
def test_retry_delay_is_within_jittered_exponential_bounds(attempt: int, seed: int) -> None:
    delay = retry_delay(attempt, random.Random(seed))
    ceiling = min(MAX_BACKOFF, BASE_BACKOFF * 2 ** min(attempt - 1, 20))
    assert min(MIN_BACKOFF, ceiling) <= delay <= ceiling


def test_retry_delay_ceiling_grows_then_caps() -> None:
    class Max(random.Random):
        def random(self) -> float:
            return 1.0

    assert retry_delay(1, Max()) == BASE_BACKOFF
    assert retry_delay(2, Max()) == BASE_BACKOFF * 2
    assert retry_delay(50, Max()) == MAX_BACKOFF


def test_retry_delay_rejects_attempt_zero() -> None:
    with pytest.raises(ValueError, match="attempt starts at 1"):
        retry_delay(0)


@pytest.mark.parametrize(
    ("attempts", "max_attempts", "retryable", "expected"),
    [
        (1, 5, True, JobStatus.FAILED),
        (5, 5, True, JobStatus.DEAD),
        (1, 5, False, JobStatus.DEAD),
    ],
)
def test_next_status_after_failure(
    attempts: int, max_attempts: int, retryable: bool, expected: JobStatus
) -> None:
    assert next_status_after_failure(attempts, max_attempts, retryable=retryable) is expected
