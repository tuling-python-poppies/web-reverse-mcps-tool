"""Domain boundaries shared by cookie and capture filters."""
from __future__ import annotations


def domain_matches(actual: str, requested: str) -> bool:
    """Match a host and its subdomains, never a substring of another host."""
    actual = actual.lower().strip().strip(".")
    requested = requested.lower().strip().strip(".")
    return bool(requested) and (
        actual == requested or actual.endswith("." + requested)
    )
