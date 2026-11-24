import pytest
from hypothesis import given
from hypothesis import strategies as st

from core.domain.data_class import DataClass, ProviderTier, classify, may_send, strictest

PUBLIC, PRIVATE = DataClass.PUBLIC, DataClass.PRIVATE


def test_classify_fails_closed() -> None:
    assert classify("public") is PUBLIC
    for value in ["private", "PUBLIC", "Public", "", None, "unknown"]:
        assert classify(value) is PRIVATE


@pytest.mark.parametrize(
    ("classes", "expected"),
    [
        ([PUBLIC, PUBLIC], PUBLIC),
        ([PUBLIC, PRIVATE], PRIVATE),
        ([PRIVATE], PRIVATE),
        ([], PRIVATE),  # nothing known about the input -> private
        (["public", None], PRIVATE),
    ],
)
def test_strictest(classes: list[DataClass | str | None], expected: DataClass) -> None:
    assert strictest(classes) is expected


def test_trusted_provider_may_see_private() -> None:
    assert may_send(ProviderTier.TRUSTED, [PRIVATE])


def test_free_provider_only_sees_all_public() -> None:
    assert may_send(ProviderTier.FREE, [PUBLIC, PUBLIC])
    assert not may_send(ProviderTier.FREE, [PUBLIC, PRIVATE])
    assert not may_send(ProviderTier.FREE, [])


def test_free_provider_rejects_private_hidden_in_a_generator() -> None:
    # One private chat turn among many public chunks must block the call.
    context = (PUBLIC for _ in range(100))
    history = iter([PRIVATE])
    assert not may_send(ProviderTier.FREE, [*context, *history])


@given(st.lists(st.sampled_from([PUBLIC, PRIVATE, "public", "private", None, "x"])))
def test_free_provider_never_receives_anything_private(
    classes: list[DataClass | str | None],
) -> None:
    if may_send(ProviderTier.FREE, classes):
        assert classes
        assert all(
            classify(c if not isinstance(c, DataClass) else c.value) is PUBLIC for c in classes
        )
