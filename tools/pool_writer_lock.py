#!/usr/bin/env python3
"""Compatibility adapter for the seed-pool mutation owner."""

try:
    from pool_mutation import pool_writer_guard
except ImportError:  # Imported as tools.pool_writer_lock.
    from tools.pool_mutation import pool_writer_guard


__all__ = ("pool_writer_guard",)
