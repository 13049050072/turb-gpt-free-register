# -*- coding: utf-8 -*-
"""Synchronous entry point for the repository's normal liveness worker."""
from __future__ import annotations


def run_liveness_check(account_id: int) -> dict:
    """Reuse normal liveness routing, persistence, logging, and concurrency claims."""
    from core.live_check_service import run_account_live_check

    return run_account_live_check(
        int(account_id),
        trigger="chatgpt2api-401",
        proxy=None,
    )


__all__ = ["run_liveness_check"]
