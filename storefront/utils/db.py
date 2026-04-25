# storefront/utils/db.py
"""
Utility helpers for safely querying optional database tables.

Some models (e.g. ProductBundle, BatchJob) live in migrations that may not
have been applied yet in every environment.  Wrapping those queries with
``safe_db_query`` prevents 500 errors and lets the rest of the page render.
"""
from django.db import DatabaseError, OperationalError, ProgrammingError
import logging

logger = logging.getLogger(__name__)

# All database-level errors that indicate a missing or inaccessible table.
_DB_ERRORS = (DatabaseError, OperationalError, ProgrammingError)


def safe_db_query(callable_or_queryset, default=None, warn=True):
    """
    Execute *callable_or_queryset* and return its result.

    If a database error is raised (e.g. because the underlying table does not
    exist yet), ``default`` is returned instead and a warning is logged.

    Usage examples::

        # Evaluate a queryset
        bundles = safe_db_query(lambda: store.bundles.count(), default=0)

        # Call any callable
        items = safe_db_query(
            lambda: list(store.bundles.filter(is_active=True)),
            default=[],
        )

    Args:
        callable_or_queryset: A zero-argument callable whose return value is
            the desired result.  Pass a ``lambda`` when you need to defer
            queryset evaluation.
        default: Value to return when a database error occurs.
        warn: If ``True`` (default), log a warning when an error is caught.

    Returns:
        The result of *callable_or_queryset*, or *default* on error.
    """
    try:
        return callable_or_queryset()
    except _DB_ERRORS as exc:
        if warn:
            logger.warning(
                "safe_db_query caught a database error (table may not exist yet). "
                "Run 'python manage.py migrate' to apply pending migrations. "
                "Error: %s",
                exc,
            )
        return default
