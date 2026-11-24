import pytest

from core.domain.errors import (
    AppError,
    BudgetExceeded,
    Conflict,
    ConnectorError,
    NotFound,
    PermissionDenied,
    RateLimited,
    Unauthenticated,
    UpstreamUnavailable,
    ValidationFailed,
)

ALL = [
    NotFound,
    PermissionDenied,
    Unauthenticated,
    ValidationFailed,
    Conflict,
    UpstreamUnavailable,
    BudgetExceeded,
]


@pytest.mark.parametrize("cls", ALL)
def test_errors_are_app_errors_with_stable_codes(cls: type[AppError]) -> None:
    err = cls("boom")
    assert isinstance(err, AppError)
    assert err.detail == "boom"
    assert err.code
    assert err.code == err.code.lower()


def test_error_codes_are_unique() -> None:
    codes = [c.code for c in [*ALL, RateLimited, ConnectorError]]
    assert len(codes) == len(set(codes))


def test_rate_limited_carries_retry_after() -> None:
    err = RateLimited("slow down", retry_after_s=20)
    assert err.retry_after_s == 20
    assert err.retryable


def test_connector_error_retryability_is_per_instance() -> None:
    assert ConnectorError("x", retryable=True, connector="github").retryable
    assert not ConnectorError("x", retryable=False, connector="github").retryable


def test_errors_chain_the_original_cause() -> None:
    def adapter() -> None:
        try:
            raise TimeoutError("vendor")
        except TimeoutError as e:
            raise UpstreamUnavailable("openai timed out") from e

    with pytest.raises(UpstreamUnavailable) as info:
        adapter()
    assert isinstance(info.value.__cause__, TimeoutError)
