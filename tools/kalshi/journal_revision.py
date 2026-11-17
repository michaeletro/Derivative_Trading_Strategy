"""Cheap, conservative identity for unchanged autocommit journal reads.

This is an invalidation token, not a transaction or an account snapshot. It
must be checked on both sides of any reusable read. Transactional callers get
no token: SQLite's counters cannot identify a rollback to an inner savepoint.
"""
from __future__ import annotations

import sqlite3


def journal_read_revision(journal):
    """Return an immutable connection/change token, or ``None`` when uncertain.

    ``total_changes`` observes this connection's row mutations, ``data_version``
    observes other connections' commits, and ``schema_version`` observes local
    DDL. Checking the external version twice refuses a change during acquisition.
    No history rows are read and no transaction is started, ended or adopted.
    """
    connection = getattr(journal, 'connection', None)
    if connection is None:
        return None
    try:
        if connection.in_transaction:
            return None
        changes = connection.total_changes
        before = connection.execute('PRAGMA data_version').fetchone()[0]
        schema = connection.execute('PRAGMA schema_version').fetchone()[0]
        after = connection.execute('PRAGMA data_version').fetchone()[0]
        if (connection.in_transaction or connection.total_changes != changes
                or before != after or getattr(journal, 'connection', None) is not connection
                or any(type(value) is not int or value < 0 for value in (changes, before, schema, after))):
            return None
        return (id(connection), changes, after, schema)
    except (sqlite3.Error, AttributeError, IndexError, TypeError):
        return None
