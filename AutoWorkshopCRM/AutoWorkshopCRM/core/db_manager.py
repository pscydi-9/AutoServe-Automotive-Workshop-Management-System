"""
core.db_manager
===============

Thread-safe SQLite persistence layer featuring:

* :class:`ConnectionPool` — a bounded, lazily-populated pool of connections
  that can be shared safely between the Tk main thread and worker threads.
* :class:`DatabaseManager` — the single gateway every model uses to talk to
  the database. It exposes two context managers:

  - :meth:`DatabaseManager.connection` for read-only work, and
  - :meth:`DatabaseManager.transaction` for atomic units of work which
    ``COMMIT`` on success and issue a full ``ROLLBACK`` on *any* exception.

Connections are opened in autocommit mode (``isolation_level=None``) so that
transaction boundaries are controlled explicitly with ``BEGIN`` /
``COMMIT`` / ``ROLLBACK`` rather than by the implicit heuristics of the
``sqlite3`` module. ``BEGIN IMMEDIATE`` acquires SQLite's RESERVED write lock
up-front, which serialises competing writers and prevents lost updates during
inventory allocation.
"""

from __future__ import annotations

import logging
import queue
import sqlite3
import threading
from contextlib import contextmanager
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterator, List, Optional, Sequence, Union

from core.config import DatabaseConfig

logger = logging.getLogger(__name__)

SQLParams = Union[Sequence[Any], dict]

# Persist Decimal values losslessly as text; NUMERIC affinity stores them as numbers.
sqlite3.register_adapter(Decimal, str)


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #
class DatabaseError(Exception):
    """Base class for all persistence-layer failures."""


class PoolExhaustedError(DatabaseError):
    """Raised when no pooled connection becomes available within the timeout."""


class TransactionError(DatabaseError):
    """Raised when a transaction fails and has been rolled back."""


class TransactionMode(str, Enum):
    """SQLite locking strategies for ``BEGIN``."""

    DEFERRED = "DEFERRED"    #: Acquire locks lazily on first read/write.
    IMMEDIATE = "IMMEDIATE"  #: Take the write lock immediately (recommended for writes).
    EXCLUSIVE = "EXCLUSIVE"  #: Block readers as well as writers.


# --------------------------------------------------------------------------- #
# Connection pool
# --------------------------------------------------------------------------- #
class ConnectionPool:
    """
    Bounded pool of reusable :class:`sqlite3.Connection` objects.

    Connections are created on demand up to ``max_size``. Callers that find
    the pool exhausted block for up to ``acquire_timeout`` seconds before a
    :class:`PoolExhaustedError` is raised.
    """

    def __init__(
        self,
        factory: Callable[[], sqlite3.Connection],
        max_size: int,
        acquire_timeout: float,
    ) -> None:
        """
        :param factory: Zero-argument callable producing a configured connection.
        :param max_size: Upper bound on simultaneously open connections.
        :param acquire_timeout: Seconds to wait for a free connection.
        """
        if max_size < 1:
            raise ValueError("Pool max_size must be >= 1.")
        self._factory = factory
        self._max_size = max_size
        self._acquire_timeout = acquire_timeout
        self._idle: "queue.LifoQueue[sqlite3.Connection]" = queue.LifoQueue(maxsize=max_size)
        self._created = 0
        self._lock = threading.Lock()
        self._closed = False

    @property
    def size(self) -> int:
        """Number of connections created so far."""
        return self._created

    def acquire(self) -> sqlite3.Connection:
        """Borrow a connection, creating one if capacity allows."""
        if self._closed:
            raise DatabaseError("Connection pool has been closed.")
        try:
            return self._idle.get_nowait()
        except queue.Empty:
            pass

        with self._lock:
            if self._created < self._max_size:
                self._created += 1
                try:
                    return self._factory()
                except Exception:
                    self._created -= 1
                    raise

        try:
            return self._idle.get(timeout=self._acquire_timeout)
        except queue.Empty as exc:
            raise PoolExhaustedError(
                f"No database connection available after {self._acquire_timeout:.1f}s "
                f"(pool size {self._max_size})."
            ) from exc

    def release(self, conn: sqlite3.Connection) -> None:
        """Return a connection to the pool, discarding it if it is unusable."""
        if self._closed:
            conn.close()
            return
        try:
            if conn.in_transaction:  # Defensive: never leak an open transaction.
                conn.rollback()
            self._idle.put_nowait(conn)
        except (sqlite3.Error, queue.Full):
            with self._lock:
                self._created -= 1
            conn.close()

    def close_all(self) -> None:
        """Close every idle connection and refuse further acquisitions."""
        self._closed = True
        while True:
            try:
                self._idle.get_nowait().close()
            except queue.Empty:
                break
        with self._lock:
            self._created = 0


# --------------------------------------------------------------------------- #
# Database manager
# --------------------------------------------------------------------------- #
class DatabaseManager:
    """
    Facade over the pool providing transactional context managers and
    convenience query helpers.

    Example::

        db = DatabaseManager(config.database)
        db.initialize_schema()
        with db.transaction(TransactionMode.IMMEDIATE) as conn:
            conn.execute("UPDATE Spare_Parts SET stock_quantity = stock_quantity - 1 "
                         "WHERE part_id = ?", (7,))
    """

    def __init__(self, config: DatabaseConfig) -> None:
        """
        :param config: Database section of :class:`core.config.AppConfig`.
        """
        self._config = config
        self._db_path: Path = Path(config.path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._pool = ConnectionPool(
            factory=self._create_connection,
            max_size=config.pool_size,
            acquire_timeout=config.pool_acquire_timeout,
        )
        logger.info("DatabaseManager ready (path=%s, pool=%d)", self._db_path, config.pool_size)

    # ------------------------------------------------------------------ #
    # Connection lifecycle
    # ------------------------------------------------------------------ #
    def _create_connection(self) -> sqlite3.Connection:
        """Open and tune a new SQLite connection."""
        conn = sqlite3.connect(
            str(self._db_path),
            timeout=self._config.busy_timeout_seconds,
            isolation_level=None,          # explicit transaction control
            check_same_thread=False,       # pool hands connections across threads
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute(f"PRAGMA busy_timeout = {int(self._config.busy_timeout_seconds * 1000)};")
        if self._config.enable_wal:
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute("PRAGMA synchronous = NORMAL;")
        return conn

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """
        Borrow a pooled connection for read-only or autocommit work.

        The connection is always returned to the pool, even on error.
        """
        conn = self._pool.acquire()
        try:
            yield conn
        finally:
            self._pool.release(conn)

    @contextmanager
    def transaction(
        self, mode: TransactionMode = TransactionMode.IMMEDIATE
    ) -> Iterator[sqlite3.Connection]:
        """
        Execute a block atomically.

        ``COMMIT`` is issued when the block exits normally. Any exception —
        domain or SQL — triggers a full ``ROLLBACK`` and is re-raised so that
        callers can translate it into a user-facing message.

        :param mode: SQLite lock acquisition strategy.
        """
        with self.connection() as conn:
            conn.execute(f"BEGIN {mode.value};")
            try:
                yield conn
            except BaseException as exc:
                try:
                    conn.execute("ROLLBACK;")
                    logger.warning("Transaction rolled back (%s: %s)", type(exc).__name__, exc)
                except sqlite3.Error:  # pragma: no cover - rollback itself failed
                    logger.exception("ROLLBACK failed; discarding connection state.")
                raise
            else:
                try:
                    conn.execute("COMMIT;")
                except sqlite3.Error as exc:
                    conn.execute("ROLLBACK;")
                    raise TransactionError(f"COMMIT failed and was rolled back: {exc}") from exc

    # ------------------------------------------------------------------ #
    # Query helpers
    # ------------------------------------------------------------------ #
    def execute(self, sql: str, params: SQLParams = ()) -> int:
        """Run a single write statement in its own transaction; return ``rowcount``."""
        with self.transaction() as conn:
            return conn.execute(sql, params).rowcount

    def execute_returning_id(self, sql: str, params: SQLParams = ()) -> int:
        """Run an ``INSERT`` in its own transaction and return ``lastrowid``."""
        with self.transaction() as conn:
            cursor = conn.execute(sql, params)
            return int(cursor.lastrowid or 0)

    def execute_many(self, sql: str, rows: Sequence[SQLParams]) -> int:
        """Bulk-execute a statement atomically; return total affected rows."""
        with self.transaction() as conn:
            return conn.executemany(sql, rows).rowcount

    def fetch_one(self, sql: str, params: SQLParams = ()) -> Optional[sqlite3.Row]:
        """Return the first row of a query or ``None``."""
        with self.connection() as conn:
            return conn.execute(sql, params).fetchone()

    def fetch_all(self, sql: str, params: SQLParams = ()) -> List[sqlite3.Row]:
        """Return every row of a query."""
        with self.connection() as conn:
            return conn.execute(sql, params).fetchall()

    def fetch_scalar(self, sql: str, params: SQLParams = (), default: Any = None) -> Any:
        """Return the first column of the first row, or ``default``."""
        row = self.fetch_one(sql, params)
        return default if row is None or row[0] is None else row[0]

    # ------------------------------------------------------------------ #
    # Schema & maintenance
    # ------------------------------------------------------------------ #
    def initialize_schema(self, schema_path: Optional[Path] = None) -> None:
        """
        Bring the database to the current schema version.

        1. If the database already holds an older schema, every migration
           script in :attr:`DatabaseConfig.migrations_dir` numbered above the
           current ``PRAGMA user_version`` runs inside one transaction
           (all-or-nothing).
        2. The idempotent DDL script (``CREATE ... IF NOT EXISTS``) is then
           applied, creating any missing tables and indexes.

        :param schema_path: Override for :attr:`DatabaseConfig.schema_path`.
        :raises DatabaseError: missing script or failed migration (rolled back).
        """
        path = Path(schema_path or self._config.schema_path)
        if not path.exists():
            raise DatabaseError(f"Schema file not found: {path}")
        with self.connection() as conn:
            current = int(conn.execute("PRAGMA user_version;").fetchone()[0])
            has_tables = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'Vehicles';"
            ).fetchone()[0] > 0
            if has_tables and current < self._config.schema_version:
                self._apply_migrations(conn, current)
            try:
                conn.executescript(path.read_text(encoding="utf-8"))
            except sqlite3.Error as exc:
                raise DatabaseError(f"Schema initialisation failed: {exc}") from exc
        logger.info("Schema v%d applied from %s", self.schema_version(), path.name)

    def _apply_migrations(self, conn: sqlite3.Connection, current: int) -> None:
        """Run ``NNN_*.sql`` scripts with ``NNN > current`` atomically, in order."""
        directory = self._config.migrations_dir
        scripts = sorted(
            (int(p.name.split("_", 1)[0]), p) for p in directory.glob("[0-9][0-9][0-9]_*.sql")
        ) if directory.exists() else []
        pending = [(version, p) for version, p in scripts if version > current]
        if not pending:
            return
        body = "\n".join(p.read_text(encoding="utf-8") for _v, p in pending)
        try:
            conn.executescript(f"BEGIN IMMEDIATE;\n{body}\nCOMMIT;")
        except sqlite3.Error as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK;")
            raise DatabaseError(f"Migration from v{current} failed and was rolled back: {exc}") from exc
        logger.info("Migrated database v%d -> v%d (%s)", current, pending[-1][0],
                    ", ".join(p.name for _v, p in pending))

    def schema_version(self) -> int:
        """Return ``PRAGMA user_version`` (set by the DDL script)."""
        return int(self.fetch_scalar("PRAGMA user_version;", default=0))

    def health_check(self) -> bool:
        """Return ``True`` if the database answers a trivial query."""
        try:
            return self.fetch_scalar("SELECT 1;") == 1
        except (sqlite3.Error, DatabaseError):
            return False

    def close(self) -> None:
        """Close all pooled connections."""
        self._pool.close_all()
        logger.info("DatabaseManager closed.")

    # Allow ``with DatabaseManager(cfg) as db:`` usage in scripts/tests.
    def __enter__(self) -> "DatabaseManager":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
