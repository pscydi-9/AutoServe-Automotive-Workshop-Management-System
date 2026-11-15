"""
models.auth_model
=================

Security state for two-step (password → one-time code) authentication and
for OTP-protected credential changes.

Components
----------
:class:`PasswordHasher`
    bcrypt hashing/verification. Plain-text passwords never reach the database
    and are never retained in memory after hashing.

:class:`LoginThrottle`
    Temporary username lockout after repeated wrong passwords.

:class:`OTPCache`
    Thread-safe runtime cache of pending one-time passwords. Codes are never
    stored in plain text: only an HMAC-SHA256 digest (keyed with
    ``SecurityConfig.secret_key`` and bound to the challenge key) is retained,
    with a monotonic expiry (≤ 5 minutes), an attempt counter and a resend
    cooldown. Challenges are keyed by *purpose* and user id, so a login code
    can never be replayed to authorise a credential change and vice versa.

:class:`SessionStore`
    Opaque, URL-safe bearer tokens. Only token digests are held in memory.

:class:`AuthModel`
    Facade over the ``Users`` table and the stores above. It owns a
    :class:`concurrent.futures.ThreadPoolExecutor`: bcrypt work, OTP issuance
    and SMTP/SMS delivery run together on a worker thread and are returned as
    :class:`~concurrent.futures.Future` objects, so the CustomTkinter main
    loop never blocks. Every SQL statement is parameterised.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
import sqlite3
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Callable, Dict, Optional, Tuple

import bcrypt

from core.config import DEFAULT_ADMIN_EMAIL, AdminSeed, SecurityConfig
from core.db_manager import DatabaseManager, TransactionMode
from core.notification_service import DispatchReceipt, NotificationService, mask_email, mask_phone

logger = logging.getLogger(__name__)

Clock = Callable[[], float]

USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_.]{3,40}$")
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
#: Strict national mobile format: exactly ten digits, nothing else.
PHONE_PATTERN = re.compile(r"^[0-9]{10}$")
BCRYPT_MAX_BYTES = 72


# --------------------------------------------------------------------------- #
# Enumerations
# --------------------------------------------------------------------------- #
class OTPChannel(str, Enum):
    """Delivery channel for a one-time password."""

    EMAIL = "email"
    SMS = "sms"


class OTPPurpose(str, Enum):
    """What a one-time password authorises."""

    LOGIN = "login"
    CREDENTIAL_CHANGE = "credential_change"


class OTPVerificationStatus(Enum):
    """Possible outcomes of :meth:`OTPCache.verify`."""

    VERIFIED = "verified"
    INVALID = "invalid"
    EXPIRED = "expired"
    NOT_FOUND = "not_found"
    LOCKED = "locked"


# --------------------------------------------------------------------------- #
# Exceptions (messages are safe to show to users)
# --------------------------------------------------------------------------- #
class AuthError(Exception):
    """Base class for authentication domain errors."""


class InvalidCredentialsError(AuthError):
    """Username/password combination rejected (deliberately non-specific)."""


class AccountLockedError(AuthError):
    """Too many wrong passwords; the username is temporarily locked."""

    def __init__(self, retry_after: int) -> None:
        super().__init__(f"Too many failed attempts. Try again in {retry_after // 60}:{retry_after % 60:02d}.")
        self.retry_after = retry_after


class OTPCooldownError(AuthError):
    """A new OTP was requested before the resend cooldown elapsed."""

    def __init__(self, retry_after: int) -> None:
        super().__init__(f"Please wait {retry_after}s before requesting a new code.")
        self.retry_after = retry_after


class CredentialValidationError(AuthError):
    """A proposed username/e-mail/phone/password failed validation."""


class OTPDeliveryError(AuthError):
    """The code could not be delivered (e.g. SMTP authentication failure)."""


class ChallengeExpiredError(AuthError):
    """The password step or pending change expired before verification."""


# --------------------------------------------------------------------------- #
# Value objects
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class UserAccount:
    """Authenticated staff member (never carries the password hash)."""

    user_id: int
    username: str
    full_name: str
    email: str
    phone: Optional[str]
    role: str


@dataclass
class OTPRecord:
    """Runtime state of one pending verification challenge."""

    key: str
    channel: OTPChannel
    code_digest: bytes = field(repr=False)
    issued_at: float
    expires_at: float
    issued_at_utc: datetime
    attempts: int = 0

    def is_expired(self, now: float) -> bool:
        """``True`` once the monotonic clock passes ``expires_at``."""
        return now >= self.expires_at

    def remaining_seconds(self, now: float) -> int:
        """Whole seconds until expiry (never negative)."""
        return max(0, int(self.expires_at - now))


@dataclass(frozen=True)
class OTPIssue:
    """Internal: a freshly issued code on its way to a transport. Never logged."""

    user: UserAccount
    purpose: OTPPurpose
    channel: OTPChannel
    destination: str
    code: str = field(repr=False)
    expires_in_seconds: int = 0


@dataclass(frozen=True)
class OTPChallenge:
    """Public result of a dispatched challenge (contains no secret)."""

    user_id: int
    purpose: OTPPurpose
    channel: OTPChannel
    destination_masked: str
    expires_in_seconds: int
    resend_cooldown_seconds: int
    is_mock: bool
    receipt: DispatchReceipt


@dataclass(frozen=True)
class CredentialChange:
    """Proposed account update submitted from the Admin Settings page."""

    username: str
    email: str
    phone: Optional[str] = None
    new_password: Optional[str] = field(default=None, repr=False)
    confirm_password: Optional[str] = field(default=None, repr=False)


@dataclass(frozen=True)
class _PendingChange:
    """Validated change waiting for its OTP. The new password is already hashed."""

    user_id: int
    username: str
    email: str
    phone: Optional[str]
    password_hash: Optional[str] = field(repr=False)
    expires_at: float = 0.0


@dataclass(frozen=True)
class Session:
    """Authenticated session handle returned to the controller layer."""

    token: str = field(repr=False)
    user: UserAccount
    created_at: datetime
    expires_at: datetime

    @property
    def is_valid(self) -> bool:
        """``True`` while the wall-clock time is before ``expires_at``."""
        return datetime.now(timezone.utc) < self.expires_at


# --------------------------------------------------------------------------- #
# Validation helpers
# --------------------------------------------------------------------------- #
def is_email(value: str) -> bool:
    """Return ``True`` if ``value`` looks like an e-mail address."""
    return bool(EMAIL_PATTERN.match(value or ""))


def is_phone(value: str) -> bool:
    """Return ``True`` only for exactly ten digits."""
    return bool(PHONE_PATTERN.match(value or ""))


def validate_password_policy(password: str, min_length: int) -> None:
    """
    Enforce the password policy.

    :raises CredentialValidationError: too short, too long for bcrypt, or
        missing a letter or a digit.
    """
    if len(password) < min_length:
        raise CredentialValidationError(f"Password must be at least {min_length} characters.")
    if len(password.encode("utf-8")) > BCRYPT_MAX_BYTES:
        raise CredentialValidationError(f"Password must be at most {BCRYPT_MAX_BYTES} bytes.")
    if not re.search(r"[A-Za-z]", password) or not re.search(r"[0-9]", password):
        raise CredentialValidationError("Password must contain at least one letter and one digit.")


# --------------------------------------------------------------------------- #
# Password hashing
# --------------------------------------------------------------------------- #
class PasswordHasher:
    """bcrypt wrapper with constant-ish timing for unknown users."""

    def __init__(self, rounds: int) -> None:
        """:param rounds: bcrypt cost factor."""
        self._rounds = rounds
        self._dummy_hash: Optional[bytes] = None

    def hash(self, password: str) -> str:
        """Return a ``$2b$`` bcrypt hash of ``password`` with a fresh salt."""
        return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(self._rounds)).decode("ascii")

    def verify(self, password: str, hashed: Optional[str]) -> bool:
        """
        Check ``password`` against ``hashed``.

        When ``hashed`` is ``None`` (unknown user) a dummy comparison is still
        performed so response time does not reveal which usernames exist.
        """
        candidate = password.encode("utf-8")
        if not hashed:
            if self._dummy_hash is None:
                self._dummy_hash = bcrypt.hashpw(b"timing-equaliser", bcrypt.gensalt(self._rounds))
            try:
                bcrypt.checkpw(candidate[:BCRYPT_MAX_BYTES], self._dummy_hash)
            except ValueError:
                pass
            return False
        try:
            return bcrypt.checkpw(candidate, hashed.encode("ascii"))
        except ValueError:  # malformed hash or over-long password
            return False


# --------------------------------------------------------------------------- #
# Brute-force protection
# --------------------------------------------------------------------------- #
class LoginThrottle:
    """Counts consecutive password failures per username and enforces a lockout."""

    def __init__(self, config: SecurityConfig, clock: Clock = time.monotonic) -> None:
        self._config = config
        self._clock = clock
        self._state: Dict[str, Tuple[int, float]] = {}
        self._lock = threading.Lock()

    def check(self, username: str) -> None:
        """:raises AccountLockedError: while ``username`` is locked."""
        key = username.lower()
        with self._lock:
            failures, locked_until = self._state.get(key, (0, 0.0))
            now = self._clock()
            if locked_until and now < locked_until:
                raise AccountLockedError(int(locked_until - now) + 1)
            if locked_until and now >= locked_until:
                self._state.pop(key, None)

    def record_failure(self, username: str) -> int:
        """Register a failure; returns the remaining attempts before lockout."""
        key = username.lower()
        with self._lock:
            failures, _ = self._state.get(key, (0, 0.0))
            failures += 1
            locked_until = 0.0
            if failures >= self._config.login_max_failures:
                locked_until = self._clock() + self._config.login_lockout_seconds
            self._state[key] = (failures, locked_until)
            return max(0, self._config.login_max_failures - failures)

    def reset(self, username: str) -> None:
        """Clear the failure counter after a successful login."""
        with self._lock:
            self._state.pop(username.lower(), None)


# --------------------------------------------------------------------------- #
# OTP cache
# --------------------------------------------------------------------------- #
class OTPCache:
    """In-memory, thread-safe OTP store with expiry, lockout and cooldown."""

    def __init__(self, config: SecurityConfig, clock: Clock = time.monotonic) -> None:
        """
        :param config: Security policy (TTL, attempts, cooldown, HMAC key).
        :param clock: Monotonic time source; injectable for deterministic tests.
        """
        self._config = config
        self._clock = clock
        self._records: Dict[str, OTPRecord] = {}
        self._lock = threading.RLock()

    def _digest(self, key: str, code: str) -> bytes:
        """HMAC-SHA256 of ``key:code`` so digests are not replayable across challenges."""
        return hmac.new(self._config.secret_key, f"{key}:{code}".encode("utf-8"),
                        hashlib.sha256).digest()

    def _generate_code(self) -> str:
        """Cryptographically secure, zero-padded numeric code (``secrets`` CSPRNG)."""
        return f"{secrets.randbelow(10 ** self._config.otp_length):0{self._config.otp_length}d}"

    def issue(self, key: str, channel: OTPChannel) -> Tuple[str, OTPRecord]:
        """
        Create (or replace) the challenge stored under ``key``.

        :raises OTPCooldownError: if called again within the resend cooldown.
        :returns: ``(plain_code, record)`` — the plain code must only be dispatched.
        """
        now = self._clock()
        with self._lock:
            self._purge_expired_locked(now)
            existing = self._records.get(key)
            if existing is not None:
                elapsed = now - existing.issued_at
                cooldown = self._config.otp_resend_cooldown_seconds
                if elapsed < cooldown:
                    raise OTPCooldownError(int(cooldown - elapsed) + 1)
            code = self._generate_code()
            record = OTPRecord(
                key=key,
                channel=channel,
                code_digest=self._digest(key, code),
                issued_at=now,
                expires_at=now + self._config.otp_ttl_seconds,
                issued_at_utc=datetime.now(timezone.utc),
            )
            self._records[key] = record
            return code, record

    def verify(self, key: str, code: str) -> OTPVerificationStatus:
        """
        Validate ``code`` in constant time.

        A successful verification consumes the challenge (single use). Too many
        failures lock the challenge, forcing the user to request a new one.
        """
        now = self._clock()
        with self._lock:
            record = self._records.get(key)
            if record is None:
                return OTPVerificationStatus.NOT_FOUND
            if record.is_expired(now):
                del self._records[key]
                return OTPVerificationStatus.EXPIRED
            if record.attempts >= self._config.otp_max_attempts:
                del self._records[key]
                return OTPVerificationStatus.LOCKED
            record.attempts += 1
            if hmac.compare_digest(record.code_digest, self._digest(key, code.strip())):
                del self._records[key]
                return OTPVerificationStatus.VERIFIED
            if record.attempts >= self._config.otp_max_attempts:
                del self._records[key]
                return OTPVerificationStatus.LOCKED
            return OTPVerificationStatus.INVALID

    def discard(self, key: str) -> None:
        """Drop the challenge under ``key`` (e.g. after a failed delivery)."""
        with self._lock:
            self._records.pop(key, None)

    def remaining_seconds(self, key: str) -> int:
        """Seconds before the active challenge expires (``0`` if none)."""
        with self._lock:
            record = self._records.get(key)
            return record.remaining_seconds(self._clock()) if record else 0

    def attempts_left(self, key: str) -> int:
        """Remaining verification attempts for the active challenge."""
        with self._lock:
            record = self._records.get(key)
            return max(0, self._config.otp_max_attempts - record.attempts) if record else 0

    def purge_expired(self) -> int:
        """Remove stale challenges; return how many were evicted."""
        with self._lock:
            return self._purge_expired_locked(self._clock())

    def _purge_expired_locked(self, now: float) -> int:
        stale = [key for key, rec in self._records.items() if rec.is_expired(now)]
        for key in stale:
            del self._records[key]
        return len(stale)


# --------------------------------------------------------------------------- #
# Session store
# --------------------------------------------------------------------------- #
class SessionStore:
    """Issues and validates opaque bearer tokens (digests held in memory only)."""

    def __init__(self, config: SecurityConfig) -> None:
        self._config = config
        self._sessions: Dict[bytes, Session] = {}
        self._lock = threading.Lock()

    def _digest(self, token: str) -> bytes:
        return hmac.new(self._config.secret_key, token.encode("utf-8"), hashlib.sha256).digest()

    def create(self, user: UserAccount) -> Session:
        """Mint a new session for ``user``."""
        token = secrets.token_urlsafe(self._config.session_token_bytes)
        now = datetime.now(timezone.utc)
        session = Session(token, user, now, now + timedelta(seconds=self._config.session_ttl_seconds))
        with self._lock:
            self._sessions[self._digest(token)] = session
        return session

    def validate(self, token: str) -> Optional[Session]:
        """Return the live session for ``token`` or ``None`` (expired sessions are evicted)."""
        key = self._digest(token)
        with self._lock:
            session = self._sessions.get(key)
            if session and not session.is_valid:
                del self._sessions[key]
                return None
            return session

    def revoke(self, token: str) -> None:
        """Invalidate ``token`` immediately."""
        with self._lock:
            self._sessions.pop(self._digest(token), None)

    def revoke_user(self, user_id: int) -> None:
        """Invalidate every session belonging to ``user_id``."""
        with self._lock:
            for key in [k for k, s in self._sessions.items() if s.user.user_id == user_id]:
                del self._sessions[key]

    def rotate(self, user: UserAccount) -> Session:
        """Revoke all of ``user``'s sessions and mint a fresh one (after credential changes)."""
        self.revoke_user(user.user_id)
        return self.create(user)


# --------------------------------------------------------------------------- #
# Facade
# --------------------------------------------------------------------------- #
class AuthModel:
    """Business rules for staff authentication and credential management."""

    _USER_COLUMNS = "user_id, username, full_name, email, phone, role"

    def __init__(self, db: DatabaseManager, config: SecurityConfig, notifier: NotificationService,
                 app_name: str = "AutoServe Pro", clock: Clock = time.monotonic) -> None:
        """
        :param db: Shared database manager.
        :param config: Security policy.
        :param notifier: Synchronous e-mail/SMS transports (run on our executor).
        :param app_name: Product name used in outbound messages.
        :param clock: Monotonic clock (injectable for tests).
        """
        self._db = db
        self._config = config
        self._notifier = notifier
        self._app_name = app_name
        self._clock = clock
        self.hasher = PasswordHasher(config.bcrypt_rounds)
        self.throttle = LoginThrottle(config, clock)
        self.otp_cache = OTPCache(config, clock)
        self.sessions = SessionStore(config)
        self._executor = ThreadPoolExecutor(max_workers=config.auth_worker_threads,
                                            thread_name_prefix="auth-worker")
        self._lock = threading.Lock()
        self._password_verified: Dict[int, float] = {}
        self._pending_changes: Dict[int, _PendingChange] = {}

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def otp_key(purpose: OTPPurpose, user_id: int) -> str:
        """Cache key binding a challenge to its purpose and account."""
        return f"{purpose.value}:{user_id}"

    def is_mock(self, channel: OTPChannel) -> bool:
        """Whether ``channel`` runs in terminal-diagnostic mock mode."""
        return self._notifier.email.is_mock if channel is OTPChannel.EMAIL else self._notifier.sms.is_mock

    def _row_to_user(self, row: sqlite3.Row) -> UserAccount:
        return UserAccount(row["user_id"], row["username"], row["full_name"], row["email"],
                           row["phone"], row["role"])

    def get_user(self, user_id: int) -> Optional[UserAccount]:
        """Fetch an *active* account by id."""
        row = self._db.fetch_one(
            f"SELECT {self._USER_COLUMNS} FROM Users WHERE user_id = ? AND is_active = 1;", (user_id,))
        return self._row_to_user(row) if row else None

    def find_by_username(self, username: str) -> Optional[UserAccount]:
        """Fetch an *active* account by username (case-insensitive)."""
        row = self._db.fetch_one(
            f"SELECT {self._USER_COLUMNS} FROM Users WHERE username = ? AND is_active = 1;",
            (username.strip(),))
        return self._row_to_user(row) if row else None

    # ------------------------------------------------------------------ #
    # Bootstrap
    # ------------------------------------------------------------------ #
    def ensure_admin(self, seed: AdminSeed) -> None:
        """
        Make sure a usable administrator exists.

        * Empty ``Users`` table → insert the seed admin with a bcrypt hash.
        * Accounts migrated from schema v1 (no username/hash) → backfilled.
        * ``ADMIN_EMAIL`` configured while the admin still has the placeholder
          address → the admin's e-mail is updated so live OTPs reach the inbox.
        """
        if not USERNAME_PATTERN.match(seed.username):
            raise CredentialValidationError("ADMIN_USERNAME must be 3–40 letters, digits, '_' or '.'.")
        phone = seed.phone.strip() or None
        if phone is not None and not is_phone(phone):
            logger.warning("ADMIN_PHONE '%s' is not 10 digits; SMS channel disabled for admin.", phone)
            phone = None
        if self._db.fetch_scalar("SELECT COUNT(*) FROM Users;", default=0) == 0:
            self._db.execute(
                "INSERT INTO Users (username, password_hash, full_name, email, phone, role) "
                "VALUES (?, ?, ?, ?, ?, 'Admin');",
                (seed.username, self.hasher.hash(seed.password), seed.full_name, seed.email.lower(), phone),
            )
            logger.info("Seed administrator '%s' created.", seed.username)
            return

        legacy = self._db.fetch_all(
            "SELECT user_id, email, role FROM Users "
            "WHERE username IS NULL OR password_hash IS NULL ORDER BY user_id;")
        admin_named = False
        for row in legacy:
            if row["role"] == "Admin" and not admin_named and self.find_by_username(seed.username) is None:
                username, admin_named = seed.username, True
            else:
                username = re.sub(r"[^A-Za-z0-9_.]", "", row["email"].split("@")[0])[:30] or "user"
                username = f"{username}{row['user_id']}" if len(username) < 3 else username
            self._db.execute(
                "UPDATE Users SET username = COALESCE(username, ?), "
                "password_hash = COALESCE(password_hash, ?), updated_at = datetime('now') "
                "WHERE user_id = ?;",
                (username, self.hasher.hash(seed.password), row["user_id"]),
            )
            logger.warning("Backfilled credentials for migrated account #%d (username '%s').",
                           row["user_id"], username)

        if seed.email_from_env and seed.email != DEFAULT_ADMIN_EMAIL:
            try:
                changed = self._db.execute(
                    "UPDATE Users SET email = ?, updated_at = datetime('now') "
                    "WHERE role = 'Admin' AND email = ?;",
                    (seed.email.lower(), DEFAULT_ADMIN_EMAIL),
                )
                if changed:
                    logger.info("Administrator e-mail synchronised from ADMIN_EMAIL.")
            except sqlite3.IntegrityError:
                logger.warning("ADMIN_EMAIL is already used by another account; not synchronised.")

    # ------------------------------------------------------------------ #
    # Step 1 — password
    # ------------------------------------------------------------------ #
    def authenticate(self, username: str, password: str) -> UserAccount:
        """
        Verify a username/password pair (blocking: bcrypt).

        :raises AccountLockedError: username temporarily locked.
        :raises InvalidCredentialsError: unknown user or wrong password.
        """
        uname = (username or "").strip()
        if not USERNAME_PATTERN.match(uname):
            self.hasher.verify(password or "", None)
            raise InvalidCredentialsError("Invalid username or password.")
        self.throttle.check(uname)
        row = self._db.fetch_one(
            f"SELECT {self._USER_COLUMNS}, password_hash FROM Users "
            "WHERE username = ? AND is_active = 1;",
            (uname,),
        )
        if not self.hasher.verify(password or "", row["password_hash"] if row else None) or row is None:
            left = self.throttle.record_failure(uname)
            hint = f" {left} attempt(s) left before a temporary lock." if 0 < left <= 2 else ""
            raise InvalidCredentialsError(f"Invalid username or password.{hint}")
        self.throttle.reset(uname)
        return self._row_to_user(row)

    def start_login(self, username: str, password: str, channel: OTPChannel) -> "Future[OTPChallenge]":
        """
        Asynchronously verify the password, issue a login OTP and deliver it.

        The returned future resolves to an :class:`OTPChallenge` or raises an
        :class:`AuthError` subclass. Nothing here touches the UI thread.
        """
        return self._executor.submit(self._start_login_blocking, username, password, channel)

    def _start_login_blocking(self, username: str, password: str,
                              channel: OTPChannel) -> OTPChallenge:
        user = self.authenticate(username, password)
        with self._lock:
            self._password_verified[user.user_id] = self._clock() + self._config.otp_ttl_seconds
        return self._issue_and_deliver(user, OTPPurpose.LOGIN, channel)

    def resend_login_otp(self, user_id: int, channel: OTPChannel) -> "Future[OTPChallenge]":
        """Re-send a login code without re-entering the password (while step 1 is fresh)."""
        return self._executor.submit(self._resend_login_blocking, user_id, channel)

    def _resend_login_blocking(self, user_id: int, channel: OTPChannel) -> OTPChallenge:
        self._require_password_step(user_id)
        user = self.get_user(user_id)
        if user is None:
            raise InvalidCredentialsError("This account is no longer active.")
        with self._lock:
            self._password_verified[user_id] = self._clock() + self._config.otp_ttl_seconds
        return self._issue_and_deliver(user, OTPPurpose.LOGIN, channel)

    def _require_password_step(self, user_id: int) -> None:
        with self._lock:
            deadline = self._password_verified.get(user_id, 0.0)
            if self._clock() >= deadline:
                self._password_verified.pop(user_id, None)
                raise ChallengeExpiredError("Sign-in step expired. Please enter your password again.")

    # ------------------------------------------------------------------ #
    # Step 2 — OTP
    # ------------------------------------------------------------------ #
    def verify_login_otp(self, user_id: int, code: str) -> Tuple[OTPVerificationStatus, Optional[Session]]:
        """Verify the login code; on success return a freshly minted :class:`Session`."""
        try:
            self._require_password_step(user_id)
        except ChallengeExpiredError:
            self.otp_cache.discard(self.otp_key(OTPPurpose.LOGIN, user_id))
            return OTPVerificationStatus.EXPIRED, None
        if not code.strip().isdigit():
            return OTPVerificationStatus.INVALID, None
        status = self.otp_cache.verify(self.otp_key(OTPPurpose.LOGIN, user_id), code)
        if status is not OTPVerificationStatus.VERIFIED:
            return status, None
        with self._lock:
            self._password_verified.pop(user_id, None)
        user = self.get_user(user_id)
        if user is None:
            return OTPVerificationStatus.NOT_FOUND, None
        logger.info("User '%s' completed two-step sign-in.", user.username)
        return status, self.sessions.create(user)

    # ------------------------------------------------------------------ #
    # Credential change (OTP challenge to the *current* e-mail)
    # ------------------------------------------------------------------ #
    def request_credential_change(self, user_id: int, current_password: str,
                                  change: CredentialChange) -> "Future[OTPChallenge]":
        """
        Asynchronously validate a proposed account update and e-mail an OTP.

        Nothing is written until :meth:`confirm_credential_change` succeeds.
        """
        return self._executor.submit(self._request_change_blocking, user_id, current_password, change)

    def _request_change_blocking(self, user_id: int, current_password: str,
                                 change: CredentialChange) -> OTPChallenge:
        row = self._db.fetch_one(
            f"SELECT {self._USER_COLUMNS}, password_hash FROM Users WHERE user_id = ? AND is_active = 1;",
            (user_id,))
        if row is None:
            raise InvalidCredentialsError("This account is no longer active.")
        if not self.hasher.verify(current_password or "", row["password_hash"]):
            raise InvalidCredentialsError("Current password is incorrect.")
        user = self._row_to_user(row)
        username, email, phone, new_password = self._validate_change(user, change)
        if new_password and self.hasher.verify(new_password, row["password_hash"]):
            raise CredentialValidationError("New password must be different from your current password.")
        if (username == user.username and email == user.email.lower() and phone == user.phone
                and not new_password):
            raise CredentialValidationError("Nothing to update — change at least one field.")

        pending = _PendingChange(
            user_id=user_id, username=username, email=email, phone=phone,
            password_hash=self.hasher.hash(new_password) if new_password else None,
            expires_at=self._clock() + self._config.otp_ttl_seconds,
        )
        with self._lock:
            self._pending_changes[user_id] = pending
        try:
            return self._issue_and_deliver(user, OTPPurpose.CREDENTIAL_CHANGE, OTPChannel.EMAIL)
        except AuthError:
            with self._lock:
                self._pending_changes.pop(user_id, None)
            raise

    def _validate_change(self, user: UserAccount,
                         change: CredentialChange) -> Tuple[str, str, Optional[str], Optional[str]]:
        username = (change.username or "").strip()
        email = (change.email or "").strip().lower()
        phone = (change.phone or "").strip() or None
        if not USERNAME_PATTERN.match(username):
            raise CredentialValidationError("Username must be 3–40 letters, digits, '_' or '.'.")
        if not is_email(email):
            raise CredentialValidationError("Enter a valid e-mail address.")
        if phone is not None and not is_phone(phone):
            raise CredentialValidationError("Phone number must be exactly 10 digits.")
        new_password = change.new_password or None
        if new_password:
            validate_password_policy(new_password, self._config.password_min_length)
            if new_password != (change.confirm_password or ""):
                raise CredentialValidationError("New password and confirmation do not match.")
        for column, value, label in (("username", username, "username"),
                                     ("email", email, "e-mail address"),
                                     ("phone", phone, "phone number")):
            if value is None:
                continue
            clash = self._db.fetch_scalar(
                f"SELECT COUNT(*) FROM Users WHERE {column} = ? AND user_id <> ?;", (value, user.user_id))
            if clash:
                raise CredentialValidationError(f"That {label} is already used by another account.")
        return username, email, phone, new_password

    def confirm_credential_change(self, user_id: int,
                                  code: str) -> Tuple[OTPVerificationStatus, Optional[Session]]:
        """
        Verify the change OTP and, only then, execute the parameterised ``UPDATE``.

        On success all of the user's sessions are rotated and the new session
        is returned.
        """
        with self._lock:
            pending = self._pending_changes.get(user_id)
            if pending is not None and self._clock() >= pending.expires_at:
                self._pending_changes.pop(user_id, None)
                pending = None
        key = self.otp_key(OTPPurpose.CREDENTIAL_CHANGE, user_id)
        if pending is None:
            self.otp_cache.discard(key)
            return OTPVerificationStatus.NOT_FOUND, None
        if not code.strip().isdigit():
            return OTPVerificationStatus.INVALID, None
        status = self.otp_cache.verify(key, code)
        if status is not OTPVerificationStatus.INVALID:
            with self._lock:
                self._pending_changes.pop(user_id, None)
        if status is not OTPVerificationStatus.VERIFIED:
            return status, None
        try:
            with self._db.transaction(TransactionMode.IMMEDIATE) as conn:
                updated = conn.execute(
                    "UPDATE Users SET username = ?, email = ?, phone = ?, "
                    "password_hash = COALESCE(?, password_hash), updated_at = datetime('now') "
                    "WHERE user_id = ? AND is_active = 1;",
                    (pending.username, pending.email, pending.phone, pending.password_hash, user_id),
                ).rowcount
                if updated != 1:
                    raise InvalidCredentialsError("This account is no longer active.")
        except sqlite3.IntegrityError as exc:
            raise CredentialValidationError(
                "That username, e-mail or phone was taken by another account meanwhile.") from exc
        user = self.get_user(user_id)
        if user is None:
            return OTPVerificationStatus.NOT_FOUND, None
        logger.info("Credentials updated for user #%d (password changed: %s).",
                    user_id, pending.password_hash is not None)
        return status, self.sessions.rotate(user)

    def cancel_credential_change(self, user_id: int) -> None:
        """Abandon a pending change and its OTP."""
        with self._lock:
            self._pending_changes.pop(user_id, None)
        self.otp_cache.discard(self.otp_key(OTPPurpose.CREDENTIAL_CHANGE, user_id))

    # ------------------------------------------------------------------ #
    # Delivery (runs on the worker thread)
    # ------------------------------------------------------------------ #
    def _issue_and_deliver(self, user: UserAccount, purpose: OTPPurpose,
                           channel: OTPChannel) -> OTPChallenge:
        destination = user.email if channel is OTPChannel.EMAIL else (user.phone or "")
        if channel is OTPChannel.SMS and not is_phone(destination):
            raise CredentialValidationError(
                "No valid 10-digit phone number on file for this account; use e-mail.")
        key = self.otp_key(purpose, user.user_id)
        code, record = self.otp_cache.issue(key, channel)
        issue = OTPIssue(user, purpose, channel, destination, code,
                         record.remaining_seconds(record.issued_at))
        receipt = self._deliver(issue)
        if not receipt.success:
            self.otp_cache.discard(key)
            raise OTPDeliveryError(f"Could not deliver the code: {receipt.detail}")
        return OTPChallenge(
            user_id=user.user_id, purpose=purpose, channel=channel,
            destination_masked=mask_email(destination) if channel is OTPChannel.EMAIL
            else mask_phone(destination),
            expires_in_seconds=issue.expires_in_seconds,
            resend_cooldown_seconds=self._config.otp_resend_cooldown_seconds,
            is_mock=self.is_mock(channel), receipt=receipt,
        )

    def _deliver(self, issue: OTPIssue) -> DispatchReceipt:
        ttl = issue.expires_in_seconds
        validity = f"{ttl // 60}:{ttl % 60:02d} min"
        app = self._app_name
        action = ("sign-in code" if issue.purpose is OTPPurpose.LOGIN
                  else "code to confirm changes to your account credentials")
        if issue.channel is OTPChannel.SMS:
            body = f"{issue.code} is your {app} {action}. Valid for {validity}. Do not share it."
            return self._notifier.sms.send(issue.destination, body)
        warning = ("" if issue.purpose is OTPPurpose.LOGIN else
                   "\nIf you did not request a credential change, sign in and change your password now.")
        text = (f"Hello {issue.user.full_name},\n\nYour {app} {action} is: {issue.code}\n\n"
                f"It expires in {validity} and can be used once.{warning}\n"
                f"If you did not request it, ignore this e-mail.")
        html = (f"<div style='font-family:Segoe UI,Arial;max-width:480px'>"
                f"<h2 style='color:#2563EB'>{app}</h2><p>Hello {issue.user.full_name},</p>"
                f"<p>Your {action} is:</p><p style='font-size:32px;letter-spacing:8px;"
                f"font-weight:700'>{issue.code}</p><p>It expires in {validity} and can be used once."
                f"</p><p style='color:#DC2626'>{warning.strip()}</p></div>")
        subject = (f"{app} sign-in code" if issue.purpose is OTPPurpose.LOGIN
                   else f"{app} — confirm account changes")
        return self._notifier.email.send(issue.destination, subject, text, html)

    # ------------------------------------------------------------------ #
    # Introspection & lifecycle
    # ------------------------------------------------------------------ #
    def remaining_seconds(self, purpose: OTPPurpose, user_id: int) -> int:
        """Seconds until the pending challenge expires (``0`` if none)."""
        return self.otp_cache.remaining_seconds(self.otp_key(purpose, user_id))

    def attempts_left(self, purpose: OTPPurpose, user_id: int) -> int:
        """Verification attempts left on the pending challenge."""
        return self.otp_cache.attempts_left(self.otp_key(purpose, user_id))

    def logout(self, token: str) -> None:
        """Revoke a session token."""
        self.sessions.revoke(token)

    def shutdown(self) -> None:
        """Stop the worker pool (pending jobs are cancelled)."""
        self._executor.shutdown(wait=False, cancel_futures=True)
