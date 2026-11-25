"""Index the synthetic corpus into the dedicated eval workspace (a composition root).

    uv run python -m evals.load

Uses the production indexing path (IngestService.index_document), so chunking and
embedding are exactly what uploads get. Idempotent: an unchanged file is skipped by
checksum and costs nothing. A first load embeds ~6k tokens with OpenAI (about $0.0001),
recorded in the usage ledger and budget-guarded like any other ingestion.
"""

from __future__ import annotations

import asyncio

from core.adapters.factories import ingest_service
from core.adapters.parsers.formats import FormatParser
from core.adapters.postgres.engine import (
    EngineKind,
    create_engine,
    create_session_factory,
    use_selector_loop_on_windows,
)
from core.adapters.postgres.evals import PostgresEvalStore
from core.adapters.postgres.sources import PostgresSourceStore
from core.adapters.postgres.uow import make_uow
from core.config import get_settings
from core.domain.data_class import DataClass
from core.domain.documents import DocumentUpsert
from core.domain.principals import PUBLIC, workspace_principal
from evals.cases import check_references, load_cases, load_corpus

EVAL_WORKSPACE_SLUG = "slate-evals"
EVAL_WORKSPACE_NAME = "Slate evals"


async def load() -> None:
    corpus = load_corpus()
    check_references(load_cases(), corpus)
    settings = get_settings()
    engine = create_engine(settings.require_database_url(direct=True), EngineKind.DIRECT)
    try:
        uow = make_uow(create_session_factory(engine))
        ingest = ingest_service(settings, uow)
        if ingest is None:
            raise SystemExit("evals.load needs OPENAI_API_KEY and the storage settings (AWS_*)")
        ws = await PostgresEvalStore(uow).ensure_workspace(EVAL_WORKSPACE_SLUG, EVAL_WORKSPACE_NAME)
        sources = PostgresSourceStore(uow)
        parser = FormatParser()
        changed = 0
        for doc in corpus:
            public = doc.visibility == "public"
            data_class = DataClass.PUBLIC if public else DataClass.PRIVATE
            source = await sources.upload_source(ws, data_class)
            acl = (PUBLIC, workspace_principal(ws)) if public else (workspace_principal(ws),)
            parsed = parser.parse(doc.body, "text/markdown")
            result = await ingest.index_document(
                ws,
                DocumentUpsert(
                    source_id=source,
                    external_id=doc.external_id,
                    uri=doc.external_id,
                    title=doc.title,
                    mime="text/markdown",
                    checksum=doc.checksum,
                    acl=acl,
                    metadata={"corpus_path": doc.path},
                ),
                parsed.sections,
                data_class=data_class.value,
            )
            changed += int(result.changed)
        print(f"eval corpus: {len(corpus)} documents, {changed} new or changed")
    finally:
        await engine.dispose()


def main() -> int:
    use_selector_loop_on_windows()
    asyncio.run(load())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
