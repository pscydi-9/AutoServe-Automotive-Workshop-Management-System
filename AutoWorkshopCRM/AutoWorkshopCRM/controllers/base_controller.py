"""
controllers.base_controller
===========================

Shared controller primitives.

Controllers sit between views and models. They translate raw widget input into
typed domain calls and translate domain exceptions into
:class:`OperationResult` objects, so views never need ``try/except`` blocks or
any knowledge of the persistence layer.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Callable, Generic, Optional, TypeVar

from core.db_manager import DatabaseError
from models.auth_model import AuthError
from models.workshop_model import WorkshopError

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: Callable that schedules a zero-arg function on the Tk main thread.
UIDispatcher = Callable[[Callable[[], None]], None]


@dataclass(frozen=True)
class OperationResult(Generic[T]):
    """Uniform success/failure envelope returned to views."""

    success: bool
    data: Optional[T] = None
    message: str = ""

    @classmethod
    def ok(cls, data: Optional[T] = None, message: str = "") -> "OperationResult[T]":
        """Build a successful result."""
        return cls(True, data, message)

    @classmethod
    def fail(cls, message: str) -> "OperationResult[T]":
        """Build a failed result carrying a user-facing message."""
        return cls(False, None, message)


class BaseController:
    """Provides :meth:`_run`, the single exception-translation boundary."""

    #: Exceptions whose message is safe to surface verbatim to the user.
    USER_FACING_ERRORS = (WorkshopError, AuthError, ValueError)

    def _run(self, action: Callable[[], T], success_message: str = "") -> OperationResult[T]:
        """
        Execute ``action`` and wrap its outcome.

        Domain/validation errors become ``fail(str(exc))``; database errors are
        logged with a traceback and reported generically.
        """
        try:
            return OperationResult.ok(action(), success_message)
        except self.USER_FACING_ERRORS as exc:
            return OperationResult.fail(str(exc))
        except (sqlite3.Error, DatabaseError):
            logger.exception("Database failure in %s", type(self).__name__)
            return OperationResult.fail("A database error occurred. All changes were rolled back.")
