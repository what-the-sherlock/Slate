"""The keyword lane's query routing (pure string logic; the SQL itself is integration-tested)."""

from __future__ import annotations

import pytest

from core.adapters.postgres.search import _tsquery_sql


@pytest.mark.parametrize(
    "query", ["what does KF-5002 mean", "paid leave", "ERR_BUDGET_42", "e-mail policy", "doctor"]
)
def test_plain_questions_match_any_word(query: str) -> None:
    sql = _tsquery_sql(query, "english", "qe")
    assert "string_agg" in sql
    assert "websearch_to_tsquery" not in sql


@pytest.mark.parametrize(
    "query", ['"paid leave"', "leave -sick", "-draft notes", "leave or holiday"]
)
def test_search_syntax_gets_strict_semantics(query: str) -> None:
    assert "websearch_to_tsquery('english', :q)" in _tsquery_sql(query, "english", "qe")


def test_user_text_never_reaches_the_sql() -> None:
    evil = "'; DROP TABLE chunks; --"
    for cfg in ("english", "simple"):
        assert "DROP" not in _tsquery_sql(evil, cfg, "qs")
