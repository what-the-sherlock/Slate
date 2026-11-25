"""Build adapters from Settings. Called only by composition roots (api/deps.py,
worker/main.py), so both wire identical adapters from the same configuration.
"""

from __future__ import annotations

from decimal import Decimal

from core.adapters.openai.embedder import OpenAIEmbedder
from core.adapters.parsers.formats import FormatParser
from core.adapters.postgres.documents import PostgresDocumentStore
from core.adapters.postgres.rate_limit import PostgresRateLimiter
from core.adapters.postgres.search import PostgresSearchIndex
from core.adapters.postgres.sources import PostgresSourceStore
from core.adapters.postgres.uow import UnitOfWork
from core.adapters.postgres.usage import PostgresGenerationStore, PostgresUsageLedger
from core.adapters.s3.blob_store import S3BlobStore, S3Settings
from core.adapters.tokenizer import TiktokenTokenizer
from core.adapters.workersai.embedder import WorkersAIEmbedder
from core.config import Settings
from core.services.ingest import IngestService
from core.services.search import SearchService


def s3_blob_store(settings: Settings) -> S3BlobStore | None:
    s = settings
    if not (s.aws_endpoint_url_s3 and s.aws_access_key_id and s.aws_secret_access_key):
        return None
    return S3BlobStore(
        S3Settings(
            endpoint_url=s.aws_endpoint_url_s3,
            region=s.aws_region or "us-east-1",
            access_key_id=s.aws_access_key_id,
            secret_access_key=s.aws_secret_access_key.get_secret_value(),
            bucket=s.uploads_bucket,
        )
    )


def openai_embedder(settings: Settings) -> OpenAIEmbedder | None:
    key = settings.openai_api_key.get_secret_value().strip() if settings.openai_api_key else ""
    if not key:  # blank counts as "not configured"
        return None
    return OpenAIEmbedder(key, model=settings.openai_embed_model, dims=settings.embed_dims)


def workersai_embedder(settings: Settings) -> WorkersAIEmbedder | None:
    """BGE-M3 on Workers AI, or None without CF_ACCOUNT_ID / CF_API_TOKEN."""
    token = settings.cf_api_token.get_secret_value().strip() if settings.cf_api_token else ""
    account = (settings.cf_account_id or "").strip()
    if not (account and token):
        return None
    return WorkersAIEmbedder(account, token)


def search_service(settings: Settings, uow: UnitOfWork) -> SearchService:
    """Hybrid search. Without an OpenAI key it runs keyword-only (no spend in dev)."""
    return SearchService(
        PostgresSearchIndex(uow),
        openai_embedder(settings),
        PostgresGenerationStore(uow),
        PostgresUsageLedger(uow),
        PostgresRateLimiter(uow),
        monthly_budget_usd=Decimal(str(settings.monthly_llm_budget_usd)),
    )


def ingest_service(settings: Settings, uow: UnitOfWork) -> IngestService | None:
    """The upload pipeline, or None when storage or the OpenAI key isn't configured."""
    blobs = s3_blob_store(settings)
    key = settings.openai_api_key.get_secret_value().strip() if settings.openai_api_key else ""
    if blobs is None or not key:  # blank counts as "not configured"
        return None
    return IngestService(
        blobs,
        PostgresDocumentStore(uow),
        PostgresSourceStore(uow),
        FormatParser(),
        TiktokenTokenizer(),
        OpenAIEmbedder(
            key,
            model=settings.openai_embed_model,
            dims=settings.embed_dims,
        ),
        PostgresGenerationStore(uow),
        PostgresUsageLedger(uow),
        monthly_budget_usd=Decimal(str(settings.monthly_llm_budget_usd)),
    )
