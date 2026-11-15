"""
core.notification_service
=========================

Outbound messaging transports for the multi-factor verification engine.

The transports are deliberately *synchronous* and return a
:class:`DispatchReceipt`. :class:`models.auth_model.AuthModel` runs them on its
:class:`concurrent.futures.ThreadPoolExecutor`, so the Tk event loop never
freezes while an SMTP handshake or SMS gateway call is in flight.

* :class:`EmailDispatcher` — real delivery through :mod:`smtplib`
  (STARTTLS or implicit SSL). Falls back to a mock when credentials are absent.
* :class:`SMSGateway` — mock-first. In mock mode it prints a structured JSON
  network-diagnostic block to ``stdout`` for prototype profiling.
"""

from __future__ import annotations

import json
import logging
import smtplib
import ssl
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Any, Dict, Optional

from core.config import SMSConfig, SMTPConfig

logger = logging.getLogger(__name__)
_stdout_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def mask_email(address: str) -> str:
    """Return ``j***n@example.com`` style masking for display/logging."""
    local, _, domain = address.partition("@")
    if not domain:
        return "***"
    visible = local[0] + "***" + (local[-1] if len(local) > 1 else "")
    return f"{visible}@{domain}"


def mask_phone(number: str) -> str:
    """Return ``+91******0000`` style masking for display/logging."""
    digits = number.strip()
    if len(digits) <= 4:
        return "*" * len(digits)
    prefix = digits[:3] if digits.startswith("+") else digits[:2]
    return prefix + "*" * (len(digits) - len(prefix) - 4) + digits[-4:]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def emit_diagnostic_block(title: str, payload: Dict[str, Any]) -> None:
    """Print a framed, pretty JSON diagnostic block to ``stdout`` (thread-safe)."""
    rule = "=" * 72
    with _stdout_lock:
        print(f"\n{rule}\n[{title}]\n{rule}", file=sys.stdout)
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str), file=sys.stdout)
        print(rule, file=sys.stdout, flush=True)


@dataclass(frozen=True)
class DispatchReceipt:
    """Outcome of a single outbound message."""

    channel: str
    recipient_masked: str
    success: bool
    provider: str
    message_id: str
    latency_ms: int
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Serialise for diagnostics."""
        return asdict(self)


# --------------------------------------------------------------------------- #
# E-mail
# --------------------------------------------------------------------------- #
class EmailDispatcher:
    """Asynchronous SMTP e-mail sender."""

    def __init__(self, config: SMTPConfig) -> None:
        """:param config: SMTP section of the application configuration."""
        self._config = config

    @property
    def is_mock(self) -> bool:
        """``True`` when messages are printed instead of sent."""
        return self._config.mock_mode

    def _build_message(
        self, recipient: str, subject: str, body_text: str, body_html: Optional[str]
    ) -> EmailMessage:
        message = EmailMessage()
        message["From"] = self._config.sender
        message["To"] = recipient
        message["Subject"] = subject
        message["Message-ID"] = f"<{uuid.uuid4().hex}@autoserve.local>"
        message.set_content(body_text)
        if body_html:
            message.add_alternative(body_html, subtype="html")
        return message

    def send(
        self, recipient: str, subject: str, body_text: str, body_html: Optional[str] = None
    ) -> DispatchReceipt:
        """
        Perform the SMTP transaction (or mock it). **Blocking** — callers run
        this on a worker thread (see :class:`models.auth_model.AuthModel`).
        """
        started = time.perf_counter()
        message = self._build_message(recipient, subject, body_text, body_html)
        masked = mask_email(recipient)

        if self.is_mock:
            receipt = DispatchReceipt("email", masked, True, "MockSMTP", message["Message-ID"],
                                      int((time.perf_counter() - started) * 1000),
                                      "Mock delivery forced (WORKSHOP_EMAIL_MOCK=1)."
                                      if self._config.force_mock else
                                      "EMAIL_SENDER / EMAIL_APP_PASSWORD not set — mock delivery.")
            emit_diagnostic_block("EMAIL MOCK DISPATCH", {
                "event": "EMAIL_DISPATCH", "mode": "MOCK", "timestamp_utc": _utc_now_iso(),
                "thread": threading.current_thread().name,
                "request": {"to": recipient, "subject": subject, "body": body_text},
                "receipt": receipt.to_dict(),
            })
            return receipt

        cfg = self._config
        try:
            context = ssl.create_default_context()
            if cfg.use_ssl:
                with smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=cfg.timeout_seconds,
                                      context=context) as server:
                    server.login(cfg.username, cfg.password)
                    server.send_message(message)
            else:
                with smtplib.SMTP(cfg.host, cfg.port, timeout=cfg.timeout_seconds) as server:
                    server.ehlo()
                    server.starttls(context=context)
                    server.ehlo()
                    server.login(cfg.username, cfg.password)
                    server.send_message(message)
            latency = int((time.perf_counter() - started) * 1000)
            logger.info("OTP e-mail delivered to %s in %d ms", masked, latency)
            return DispatchReceipt("email", masked, True, f"SMTP:{cfg.host}",
                                   message["Message-ID"], latency, "Delivered")
        except (smtplib.SMTPException, OSError) as exc:
            latency = int((time.perf_counter() - started) * 1000)
            logger.error("SMTP delivery to %s failed: %s", masked, exc)
            return DispatchReceipt("email", masked, False, f"SMTP:{cfg.host}",
                                   message["Message-ID"], latency, f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------- #
# SMS
# --------------------------------------------------------------------------- #
class SMSGateway:
    """Asynchronous SMS sender with a profiling-friendly mock mode."""

    def __init__(self, config: SMSConfig) -> None:
        """:param config: SMS section of the application configuration."""
        self._config = config

    @property
    def is_mock(self) -> bool:
        """``True`` when SMS traffic is simulated."""
        return self._config.mock_mode

    def send(self, phone: str, body: str) -> DispatchReceipt:
        """Deliver (or simulate) one SMS. **Blocking** — run on a worker thread."""
        started = time.perf_counter()
        message_id = f"SM{uuid.uuid4().hex[:24].upper()}"
        masked = mask_phone(phone)

        if not self.is_mock:
            # Integration seam: plug a real provider SDK (Twilio, MSG91, SNS) here.
            return DispatchReceipt("sms", masked, False, self._config.provider_name, message_id, 0,
                                   "Live SMS provider not configured; enable WORKSHOP_SMS_MOCK=1.")

        time.sleep(self._config.simulated_latency_ms / 1000)  # emulate network RTT
        latency = int((time.perf_counter() - started) * 1000)
        receipt = DispatchReceipt("sms", masked, True, self._config.provider_name, message_id,
                                  latency, "Accepted by mock gateway")
        emit_diagnostic_block("SMS MOCK GATEWAY — NETWORK DIAGNOSTIC", {
            "event": "SMS_DISPATCH",
            "mode": "MOCK",
            "timestamp_utc": _utc_now_iso(),
            "thread": threading.current_thread().name,
            "request": {
                "method": "POST",
                "endpoint": "https://mock-sms.local/v1/messages",
                "headers": {"Content-Type": "application/json", "Authorization": "Bearer ***"},
                "payload": {"from": self._config.sender_id, "to": phone, "body": body},
            },
            "response": {"http_status": 202, "message_id": message_id, "status": "queued"},
            "metrics": {"latency_ms": latency, "segments": max(1, -(-len(body) // 160)),
                        "encoding": "GSM-7"},
        })
        return receipt


# --------------------------------------------------------------------------- #
# Facade
# --------------------------------------------------------------------------- #
class NotificationService:
    """
    Groups the e-mail and SMS transports.

    Transports are synchronous; thread-pool scheduling is owned by the caller
    (:class:`models.auth_model.AuthModel`) so that hashing, OTP issuance and
    delivery run together off the UI thread.
    """

    def __init__(self, smtp: SMTPConfig, sms: SMSConfig) -> None:
        """
        :param smtp: SMTP configuration.
        :param sms: SMS configuration.
        """
        self.email = EmailDispatcher(smtp)
        self.sms = SMSGateway(sms)
