"""
Pydantic request/response schemas for the RepoPilot API.
"""

from typing import Literal
from pydantic import BaseModel, field_validator
import re


class RunRequest(BaseModel):
    pr_url: str

    @field_validator("pr_url")
    @classmethod
    def validate_pr_url(cls, v: str) -> str:
        pattern = r"https?://github\.com/[^/]+/[^/]+/pull/\d+"
        if not re.match(pattern, v.strip(), re.IGNORECASE):
            raise ValueError(
                "pr_url must be a valid GitHub PR URL: "
                "https://github.com/owner/repo/pull/123"
            )
        return v.strip()


class RunResponse(BaseModel):
    run_id: str
    status: Literal["started"]
    message: str


class SSEEvent(BaseModel):
    node: str
    status: Literal["started", "done", "error"]
    summary: str | None = None
    run_id: str | None = None


class RunResult(BaseModel):
    run_id: str
    run_status: str
    report: dict
    error: str | None = None
