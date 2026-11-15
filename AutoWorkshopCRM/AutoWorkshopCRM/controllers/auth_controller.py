"""
controllers.auth_controller
===========================

Orchestrates two-step sign-in and OTP-protected credential changes.

Sign-in
-------
1. :meth:`AuthController.begin_login` hands username, password and channel to
   :meth:`models.auth_model.AuthModel.start_login`. bcrypt, OTP issuance and
   SMTP/SMS delivery all run on the model's thread pool. The returned
   :class:`~concurrent.futures.Future` is bridged back to the Tk main thread
   through the injected :data:`UIDispatcher`, so the window never freezes.
2. :meth:`AuthController.verify_login_code` checks the 6-digit code and stores
   the resulting :class:`~models.auth_model.Session`.

Credential change
-----------------
:meth:`request_credential_change` validates the proposal and e-mails a fresh
OTP to the *current* address. :meth:`confirm_credential_change` applies the SQL
``UPDATE`` only after that code is verified.
"""

from __future__ import annotations

import logging
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Callable, Optional

from controllers.base_controller import BaseController, OperationResult, UIDispatcher
from core.config import AppConfig
from models.auth_model import (
    AuthModel,
    CredentialChange,
    OTPChallenge,
    OTPChannel,
    OTPPurpose,
    OTPVerificationStatus,
    Session,
    UserAccount,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OTPDispatchInfo:
    """What a view needs to know after a code was sent."""

    channel: OTPChannel
    destination_masked: str
    expires_in_seconds: int
    resend_cooldown_seconds: int
    is_mock: bool


DispatchCallback = Callable[[OperationResult[OTPDispatchInfo]], None]


class AuthController(BaseController):
    """Mediates between the login/settings views and :class:`AuthModel`."""

    _STATUS_MESSAGES = {
        OTPVerificationStatus.INVALID: "Incorrect code. {left} attempt(s) remaining.",
        OTPVerificationStatus.EXPIRED: "This code has expired. Please request a new one.",
        OTPVerificationStatus.NOT_FOUND: "No active code. Please request a new one.",
        OTPVerificationStatus.LOCKED: "Too many failed attempts. Request a new code.",
    }

    def __init__(self, model: AuthModel, ui_dispatch: UIDispatcher, config: AppConfig) -> None:
        """
        :param model: Authentication domain model (owns the worker pool).
        :param ui_dispatch: Schedules callbacks on the UI thread.
        :param config: Application configuration.
        """
        self._model = model
        self._dispatch = ui_dispatch
        self._config = config
        self._session: Optional[Session] = None
        self._pending_login_user: Optional[int] = None

    # ------------------------------------------------------------------ #
    # Session state
    # ------------------------------------------------------------------ #
    @property
    def session(self) -> Optional[Session]:
        """Currently authenticated session, if still valid."""
        if self._session and self._model.sessions.validate(self._session.token):
            return self._session
        self._session = None
        return None

    @property
    def current_user(self) -> Optional[UserAccount]:
        """User behind the active session."""
        session = self.session
        return session.user if session else None

    @property
    def is_authenticated(self) -> bool:
        """``True`` while a valid session exists."""
        return self.session is not None

    @property
    def default_username(self) -> str:
        """Pre-filled username (seed admin) for convenience."""
        return self._config.admin.username

    @property
    def demo_credentials_hint(self) -> str:
        """Hint shown on the login screen while the demo password is in use."""
        admin = self._config.admin
        return f"Demo login: {admin.username} / {admin.password}" if admin.uses_default_password else ""

    @property
    def otp_length(self) -> int:
        """Number of digits in a one-time code."""
        return self._config.security.otp_length

    def channel_is_mock(self, channel: OTPChannel) -> bool:
        """Whether ``channel`` is running in mock/diagnostic mode."""
        return self._model.is_mock(channel)

    # ------------------------------------------------------------------ #
    # Future → UI bridge
    # ------------------------------------------------------------------ #
    def _bridge(self, future: "Future[OTPChallenge]", on_complete: DispatchCallback,
                on_success: Optional[Callable[[OTPChallenge], None]] = None) -> None:
        """Translate a worker future into an :class:`OperationResult` on the UI thread."""

        def _done(fut: "Future[OTPChallenge]") -> None:
            result: OperationResult[OTPDispatchInfo]
            try:
                challenge = fut.result()
                if on_success:
                    on_success(challenge)
                info = OTPDispatchInfo(challenge.channel, challenge.destination_masked,
                                       challenge.expires_in_seconds,
                                       challenge.resend_cooldown_seconds, challenge.is_mock)
                result = OperationResult.ok(info, f"Code sent to {info.destination_masked}.")
            except self.USER_FACING_ERRORS as exc:
                result = OperationResult.fail(str(exc))
            except Exception:  # noqa: BLE001 - never let a worker error vanish silently
                logger.exception("Background authentication task failed")
                result = OperationResult.fail("Unexpected error while processing the request.")
            self._dispatch(lambda: on_complete(result))

        future.add_done_callback(_done)

    def _verification_failure(self, status: OTPVerificationStatus, purpose: OTPPurpose,
                              user_id: int) -> str:
        left = self._model.attempts_left(purpose, user_id)
        return self._STATUS_MESSAGES.get(status, "Verification failed.").format(left=left)

    # ------------------------------------------------------------------ #
    # Sign-in
    # ------------------------------------------------------------------ #
    def begin_login(self, username: str, password: str, channel: OTPChannel,
                    on_complete: DispatchCallback) -> None:
        """
        Step 1: verify the password and dispatch a login OTP (non-blocking).

        ``on_complete`` is always invoked on the UI thread exactly once.
        """
        if not username.strip() or not password:
            self._dispatch(lambda: on_complete(OperationResult.fail("Enter your username and password.")))
            return
        self._pending_login_user = None

        def _remember(challenge: OTPChallenge) -> None:
            self._pending_login_user = challenge.user_id

        self._bridge(self._model.start_login(username, password, channel), on_complete, _remember)

    def resend_login_code(self, channel: OTPChannel, on_complete: DispatchCallback) -> None:
        """Send another login code while the password step is still fresh."""
        if self._pending_login_user is None:
            self._dispatch(lambda: on_complete(OperationResult.fail("Enter your password again.")))
            return
        self._bridge(self._model.resend_login_otp(self._pending_login_user, channel), on_complete)

    def verify_login_code(self, code: str) -> OperationResult[Session]:
        """Step 2: validate the code and establish a session on success."""
        def _verify() -> Session:
            user_id = self._pending_login_user
            if user_id is None:
                raise ValueError("Start by entering your username and password.")
            status, session = self._model.verify_login_otp(user_id, code)
            if status is OTPVerificationStatus.VERIFIED and session is not None:
                self._session = session
                self._pending_login_user = None
                return session
            raise ValueError(self._verification_failure(status, OTPPurpose.LOGIN, user_id))
        return self._run(_verify, "Signed in successfully.")

    def login_seconds_remaining(self) -> int:
        """Seconds until the pending login code expires."""
        if self._pending_login_user is None:
            return 0
        return self._model.remaining_seconds(OTPPurpose.LOGIN, self._pending_login_user)

    def cancel_login(self) -> None:
        """Return to step 1 (the issued code is discarded)."""
        if self._pending_login_user is not None:
            self._model.otp_cache.discard(self._model.otp_key(OTPPurpose.LOGIN, self._pending_login_user))
        self._pending_login_user = None

    # ------------------------------------------------------------------ #
    # Credential change (Admin Settings)
    # ------------------------------------------------------------------ #
    def request_credential_change(self, current_password: str, username: str, email: str,
                                  phone: str, new_password: str, confirm_password: str,
                                  on_complete: DispatchCallback) -> None:
        """Validate a proposed update and e-mail an OTP to the current address (non-blocking)."""
        user = self.current_user
        if user is None:
            self._dispatch(lambda: on_complete(OperationResult.fail("Your session has expired.")))
            return
        if not current_password:
            self._dispatch(lambda: on_complete(OperationResult.fail("Enter your current password.")))
            return
        change = CredentialChange(username, email, phone or None, new_password or None,
                                  confirm_password or None)
        self._bridge(self._model.request_credential_change(user.user_id, current_password, change),
                     on_complete)

    def confirm_credential_change(self, code: str) -> OperationResult[Session]:
        """Verify the change OTP; the ``UPDATE`` runs only after verification."""
        def _confirm() -> Session:
            user = self.current_user
            if user is None:
                raise ValueError("Your session has expired. Sign in again.")
            status, session = self._model.confirm_credential_change(user.user_id, code)
            if status is OTPVerificationStatus.VERIFIED and session is not None:
                self._session = session
                return session
            raise ValueError(self._verification_failure(status, OTPPurpose.CREDENTIAL_CHANGE,
                                                        user.user_id))
        return self._run(_confirm, "Account credentials updated.")

    def change_seconds_remaining(self) -> int:
        """Seconds until the pending credential-change code expires."""
        user = self.current_user
        return self._model.remaining_seconds(OTPPurpose.CREDENTIAL_CHANGE, user.user_id) if user else 0

    def cancel_credential_change(self) -> None:
        """Abandon a pending credential change."""
        user = self.current_user
        if user is not None:
            self._model.cancel_credential_change(user.user_id)

    # ------------------------------------------------------------------ #
    def logout(self) -> None:
        """Revoke the current session."""
        if self._session:
            self._model.logout(self._session.token)
        self._session = None
        self._pending_login_user = None
