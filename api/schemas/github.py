from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field

from core.services.github import MAX_REPOS_PER_CONNECT


class GitHubStatusOut(BaseModel):
    available: bool = Field(description="False until the server's GitHub App is configured")


class InstallLinkOut(BaseModel):
    url: str


class InstallationBody(BaseModel):
    installation_id: int = Field(gt=0)
    code: str = Field(min_length=1, max_length=200)
    state: str = Field(min_length=1, max_length=2000)


class GrantOut(BaseModel):
    grant: str = Field(description="Short-lived proof that this installation is yours")


class GrantBody(BaseModel):
    grant: str = Field(min_length=1, max_length=2000)


class RepoOut(BaseModel):
    id: int
    full_name: str
    private: bool
    description: str | None
    html_url: str
    connected: bool


class ConnectBody(BaseModel):
    grant: str = Field(min_length=1, max_length=2000)
    repos: list[str] = Field(min_length=1, max_length=MAX_REPOS_PER_CONNECT)


class ConnectedOut(BaseModel):
    source_id: UUID
    name: str
    job_id: UUID
