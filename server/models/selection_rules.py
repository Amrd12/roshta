"""
SelectionRules — domain helpers for skip/include toggle logic.

Applied by QueueService when it handles POST /selection.
Pure functions with no Flask or SQLite dependency.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List


@dataclass
class SelectionChange:
    """
    Represents a validated /selection request.

    Attributes:
        rev:     Client-side revision at the time of the request.
                 Rejected with 409 if it doesn't match server rev.
        skip:    List of holding_files IDs the operator wants to exclude.
        include: List of holding_files IDs the operator wants to un-exclude.
    """
    rev: int
    skip: List[str]
    include: List[str]

    @classmethod
    def from_payload(cls, payload: dict) -> "SelectionChange":
        """
        Parse and validate a /selection request payload.

        Raises:
            ValueError: if required fields are missing or malformed.
        """
        if not isinstance(payload, dict):
            raise ValueError("Payload must be a JSON object.")

        rev = payload.get("rev")
        if rev is None or not isinstance(rev, int):
            raise ValueError("'rev' must be an integer.")

        skip = payload.get("skip", [])
        include = payload.get("include", [])

        if not isinstance(skip, list) or not all(isinstance(i, str) for i in skip):
            raise ValueError("'skip' must be a list of string IDs.")
        if not isinstance(include, list) or not all(isinstance(i, str) for i in include):
            raise ValueError("'include' must be a list of string IDs.")

        overlap = set(skip) & set(include)
        if overlap:
            raise ValueError(
                f"IDs cannot appear in both 'skip' and 'include': {sorted(overlap)}"
            )

        return cls(rev=rev, skip=list(skip), include=list(include))
