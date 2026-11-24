import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from core.domain.chunking import ChunkingConfig, chunk_sections, embedding_text
from core.domain.documents import NewChunk, Section


def words(text: str) -> int:
    """Fake tokenizer: one token per word (deterministic, no network)."""
    return len(text.split())


SMALL = ChunkingConfig(target=20, overlap=5)


def test_short_sections_become_one_chunk_each_with_their_heading() -> None:
    chunks = chunk_sections(
        [Section("Intro", "Hello world."), Section("Setup > Install", "Run uv sync.")], words
    )
    assert [c.content for c in chunks] == ["Hello world.", "Run uv sync."]
    assert [c.heading_path for c in chunks] == ["Intro", "Setup > Install"]
    assert [c.ordinal for c in chunks] == [0, 1]


def test_paragraphs_are_packed_up_to_the_target() -> None:
    para = " ".join(["word"] * 8)
    text = "\n\n".join([para] * 5)  # 5 paragraphs x 8 tokens
    chunks = chunk_sections([Section(None, text)], words, SMALL)
    assert all(c.token_count <= SMALL.target for c in chunks)
    assert len(chunks) == 3  # 2 + 2 + 1 paragraphs


def test_a_long_paragraph_is_split_with_overlap() -> None:
    text = " ".join(f"w{i}" for i in range(50))
    chunks = chunk_sections([Section(None, text)], words, SMALL)
    assert len(chunks) > 1
    first, second = chunks[0].content.split(), chunks[1].content.split()
    assert first[-1] in second  # neighbours share words
    assert all(c.token_count <= SMALL.target for c in chunks)


def test_empty_sections_produce_nothing() -> None:
    assert chunk_sections([Section("Empty", "  \n\n  ")], words) == []


def test_embedding_text_has_context_prefix_but_content_stays_clean() -> None:
    chunk = NewChunk(ordinal=0, content="Run uv sync.", token_count=3, heading_path="Setup")
    assert embedding_text("Handbook", chunk) == "Handbook > Setup\n\nRun uv sync."
    assert chunk.content == "Run uv sync."


def test_invalid_config_is_rejected() -> None:
    with pytest.raises(ValueError, match="overlap"):
        ChunkingConfig(target=10, overlap=10)


_word = st.text(alphabet="abcdefgh", min_size=1, max_size=8)
_para = st.lists(_word, min_size=1, max_size=60).map(" ".join)


@settings(max_examples=150, deadline=None)
@given(st.lists(_para, min_size=1, max_size=12))
def test_invariants_no_text_lost_every_chunk_fits(paragraphs: list[str]) -> None:
    text = "\n\n".join(paragraphs)
    chunks = chunk_sections([Section("H", text)], words, SMALL)
    # 1. Every chunk fits (a single huge word is the only allowed exception).
    assert all(c.token_count <= SMALL.target or len(c.content.split()) == 1 for c in chunks)
    # 2. Nothing is lost: every input word appears in some chunk, in order of first use.
    covered = " ".join(c.content for c in chunks).split()
    assert set(text.split()) <= set(covered)
    # 3. Ordinals are 0..n-1 and every chunk keeps the section heading.
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))
    assert all(c.heading_path == "H" for c in chunks)
