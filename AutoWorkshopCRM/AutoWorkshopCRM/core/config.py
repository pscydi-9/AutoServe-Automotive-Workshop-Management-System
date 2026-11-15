"""
core.config
===========

Centralised, immutable configuration for the Automotive Workshop CRM.

Every tunable value used anywhere in the application (database paths, pool
sizing, OTP security policy, SMTP credentials, SMS mock toggle, theme
palettes, responsive breakpoints and business rules) lives here so that no
other module contains "magic numbers".

Values are resolved once from environment variables (with safe defaults) and
frozen into dataclasses. Use :func:`get_config` to obtain the process-wide
singleton.

Environment variables (also read from an optional ``.env`` file in the project root)
-----------------------------------------------------------------------------------
EMAIL_SENDER                  Gmail address that sends OTP e-mails (SMTP login).
EMAIL_APP_PASSWORD            16-character Google App Password (spaces ignored).
ADMIN_EMAIL                   Real inbox of the administrator (receives OTPs).
ADMIN_USERNAME / ADMIN_PASSWORD  Seed administrator login (default admin / Admin@123).
ADMIN_PHONE                   Seed administrator 10-digit mobile (SMS mock channel).
WORKSHOP_DB_PATH              SQLite database file path.
WORKSHOP_DB_POOL_SIZE         Max pooled connections (default 5).
WORKSHOP_SECRET_KEY           Hex/str key used to HMAC OTP codes & session tokens.
WORKSHOP_SMTP_HOST / _PORT    SMTP relay (default smtp.gmail.com:587).
WORKSHOP_SMTP_USER / _PASSWORD  Legacy aliases of EMAIL_SENDER / EMAIL_APP_PASSWORD.
WORKSHOP_SMTP_SENDER          From address (defaults to EMAIL_SENDER).
WORKSHOP_SMTP_USE_SSL         "1" for implicit TLS (port 465) instead of STARTTLS.
WORKSHOP_EMAIL_MOCK           "1" to force mock e-mail (auto when no credentials).
WORKSHOP_SMS_MOCK             "0" to disable the SMS mock (default "1").
WORKSHOP_ADMIN_NAME           Display name of the seed administrator.
WORKSHOP_APPEARANCE           "dark" | "light" | "system".
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Final, Tuple

# --------------------------------------------------------------------------- #
# Filesystem anchors
# --------------------------------------------------------------------------- #
BASE_DIR: Final[Path] = Path(__file__).resolve().parent.parent
DATABASE_DIR: Final[Path] = BASE_DIR / "database"
SQLITE_SCHEMA_PATH: Final[Path] = DATABASE_DIR / "schema_sqlite.sql"
MIGRATIONS_DIR: Final[Path] = DATABASE_DIR / "migrations"
ENV_FILE_PATH: Final[Path] = BASE_DIR / ".env"
#: Schema version the application code expects (``PRAGMA user_version``).
SCHEMA_VERSION: Final[int] = 2
DEFAULT_ADMIN_EMAIL: Final[str] = "admin@autoserve.local"
DEFAULT_ADMIN_PASSWORD: Final[str] = "Admin@123"
POSTGRES_SCHEMA_PATH: Final[Path] = DATABASE_DIR / "schema_postgresql.sql"

#: A colour that adapts to CustomTkinter's appearance mode: ``(light, dark)``.
ThemeColor = Tuple[str, str]


# --------------------------------------------------------------------------- #
# Environment helpers
# --------------------------------------------------------------------------- #
def load_env_file(path: Path = ENV_FILE_PATH) -> None:
    """
    Load ``KEY=VALUE`` lines from ``path`` into :data:`os.environ`.

    Real environment variables always win (``setdefault``). Blank lines and
    ``#`` comments are ignored; surrounding single/double quotes are stripped.
    This keeps secrets such as ``EMAIL_APP_PASSWORD`` out of the source tree.
    """
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def _env_first(keys: Tuple[str, ...], default: str) -> str:
    """Return the first non-blank value among ``keys`` (preferred name first)."""
    for key in keys:
        value = os.environ.get(key, "").strip()
        if value:
            return value
    return default


def _env_str(key: str, default: str) -> str:
    """Return a stripped environment string or ``default`` when unset/blank."""
    value = os.environ.get(key, "").strip()
    return value or default


def _env_int(key: str, default: int) -> int:
    """Return an integer environment value, falling back on parse failure."""
    try:
        return int(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


def _env_bool(key: str, default: bool) -> bool:
    """Interpret ``1/true/yes/on`` (case-insensitive) as ``True``."""
    raw = os.environ.get(key)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _resolve_secret_key() -> bytes:
    """
    Resolve the HMAC key used to hash OTP codes and session tokens.

    When ``WORKSHOP_SECRET_KEY`` is not provided an ephemeral 256-bit key is
    generated. That is safe because OTPs and sessions only live in the
    runtime cache and never outlive the process.
    """
    configured = os.environ.get("WORKSHOP_SECRET_KEY", "").strip()
    return configured.encode("utf-8") if configured else secrets.token_bytes(32)


# --------------------------------------------------------------------------- #
# Configuration sections
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DatabaseConfig:
    """Connection and pooling parameters for :class:`core.db_manager.DatabaseManager`."""

    path: Path
    pool_size: int = 5
    #: Seconds SQLite waits on a locked database before raising ``OperationalError``.
    busy_timeout_seconds: float = 10.0
    #: Seconds a caller waits for a free pooled connection.
    pool_acquire_timeout: float = 5.0
    enable_wal: bool = True
    schema_path: Path = SQLITE_SCHEMA_PATH
    migrations_dir: Path = MIGRATIONS_DIR
    schema_version: int = SCHEMA_VERSION


@dataclass(frozen=True)
class SecurityConfig:
    """Multi-factor verification and session policy."""

    secret_key: bytes = field(repr=False)
    otp_length: int = 6
    #: Hard expiry window for a one-time password (must remain < 5 minutes).
    otp_ttl_seconds: int = 299
    otp_max_attempts: int = 5
    otp_resend_cooldown_seconds: int = 30
    session_ttl_seconds: int = 8 * 60 * 60
    session_token_bytes: int = 32
    #: bcrypt cost factor (2^rounds iterations). 12 ≈ 250 ms on a laptop.
    bcrypt_rounds: int = 12
    password_min_length: int = 8
    #: Consecutive wrong passwords before the username is temporarily locked.
    login_max_failures: int = 5
    login_lockout_seconds: int = 300
    #: Worker threads used for bcrypt hashing and SMTP/SMS dispatch.
    auth_worker_threads: int = 4

    def __post_init__(self) -> None:
        if not 0 < self.otp_ttl_seconds <= 300:
            raise ValueError("otp_ttl_seconds must not exceed 5 minutes (300 s).")
        if not 4 <= self.otp_length <= 10:
            raise ValueError("otp_length must be between 4 and 10 digits.")
        if not 4 <= self.bcrypt_rounds <= 16:
            raise ValueError("bcrypt_rounds must be between 4 and 16.")


@dataclass(frozen=True)
class SMTPConfig:
    """Outbound e-mail relay used by the OTP e-mail channel."""

    host: str
    port: int
    username: str
    password: str = field(repr=False)
    sender: str
    use_ssl: bool = False
    timeout_seconds: float = 15.0
    force_mock: bool = False

    @property
    def is_configured(self) -> bool:
        """``True`` when enough credentials exist to attempt a real SMTP session (live Gmail)."""
        return bool(self.host and self.username and self.password)

    @property
    def mock_mode(self) -> bool:
        """E-mail is mocked when forced or when credentials are missing."""
        return self.force_mock or not self.is_configured


@dataclass(frozen=True)
class SMSConfig:
    """Phone verification channel. Mock mode prints JSON diagnostics to stdout."""

    mock_mode: bool = True
    provider_name: str = "MockSMSGateway/1.0"
    sender_id: str = "WKSHOP"
    simulated_latency_ms: int = 350


@dataclass(frozen=True)
class ThemePalette:
    """Adaptive ``(light, dark)`` colour tokens consumed by ``views.components``."""

    primary: ThemeColor = ("#2563EB", "#3B82F6")
    primary_hover: ThemeColor = ("#1D4ED8", "#2563EB")
    accent: ThemeColor = ("#F97316", "#FB923C")
    success: ThemeColor = ("#16A34A", "#22C55E")
    warning: ThemeColor = ("#D97706", "#F59E0B")
    danger: ThemeColor = ("#DC2626", "#EF4444")
    background: ThemeColor = ("#F1F5F9", "#0B1120")
    surface: ThemeColor = ("#FFFFFF", "#111827")
    surface_alt: ThemeColor = ("#E2E8F0", "#1F2937")
    border: ThemeColor = ("#CBD5E1", "#334155")
    text_primary: ThemeColor = ("#0F172A", "#F8FAFC")
    text_muted: ThemeColor = ("#64748B", "#94A3B8")
    text_on_primary: ThemeColor = ("#FFFFFF", "#FFFFFF")

    def status_color(self, status: str) -> ThemeColor:
        """Map a job/mechanic status label to a semantic colour."""
        mapping = {
            "Pending": self.warning,
            "In Progress": self.primary,
            "Completed": self.success,
            "Cancelled": self.danger,
            "Available": self.success,
            "Busy": self.warning,
            "Off-Duty": self.text_muted,
        }
        return mapping.get(status, self.text_muted)


@dataclass(frozen=True)
class TypographyConfig:
    """Font family and type scale."""

    family: str = "Segoe UI"
    size_display: int = 26
    size_title: int = 20
    size_heading: int = 16
    size_body: int = 13
    size_caption: int = 11


@dataclass(frozen=True)
class LayoutConfig:
    """Responsive breakpoints and spacing tokens (pixels)."""

    default_geometry: str = "1280x800"
    min_width: int = 360
    min_height: int = 560
    #: Below this width the UI switches to the single-column "mobile" layout.
    mobile_breakpoint: int = 760
    #: Below this width (and above mobile) a two-column "tablet" layout is used.
    tablet_breakpoint: int = 1150
    corner_radius: int = 14
    padding_lg: int = 24
    padding_md: int = 14
    padding_sm: int = 8
    sidebar_width: int = 220
    #: Widget scaling applied automatically on small (Pydroid/phone) screens.
    mobile_widget_scaling: float = 0.9


@dataclass(frozen=True)
class BusinessRules:
    """Domain constants shared by models, controllers and views."""

    currency_symbol: str = "₹"
    low_stock_threshold: int = 5
    job_statuses: Tuple[str, ...] = ("Pending", "In Progress", "Completed", "Cancelled")
    #: Jobs in these states may still receive spare-part allocations.
    active_job_statuses: Tuple[str, ...] = ("Pending", "In Progress")
    mechanic_statuses: Tuple[str, ...] = ("Available", "Busy", "Off-Duty")
    user_roles: Tuple[str, ...] = ("Admin", "Advisor", "Mechanic")


@dataclass(frozen=True)
class AdminSeed:
    """Bootstrap administrator inserted on first launch so login is possible."""

    full_name: str
    username: str
    password: str = field(repr=False)
    email: str
    phone: str
    #: ``True`` when ADMIN_EMAIL was explicitly configured (syncs a placeholder address).
    email_from_env: bool = False

    @property
    def uses_default_password(self) -> bool:
        """Whether the well-known demo password is in effect (UI shows a hint)."""
        return self.password == DEFAULT_ADMIN_PASSWORD


@dataclass(frozen=True)
class AppConfig:
    """Root configuration aggregate."""

    app_name: str
    version: str
    appearance_mode: str
    database: DatabaseConfig
    security: SecurityConfig
    smtp: SMTPConfig
    sms: SMSConfig
    admin: AdminSeed
    theme: ThemePalette = field(default_factory=ThemePalette)
    typography: TypographyConfig = field(default_factory=TypographyConfig)
    layout: LayoutConfig = field(default_factory=LayoutConfig)
    business: BusinessRules = field(default_factory=BusinessRules)

    @classmethod
    def from_environment(cls) -> "AppConfig":
        """Build a fully-resolved configuration from ``.env`` + ``os.environ``."""
        load_env_file()
        smtp_user = _env_first(("EMAIL_SENDER", "WORKSHOP_SMTP_USER"), "")
        app_password = _env_first(("EMAIL_APP_PASSWORD", "WORKSHOP_SMTP_PASSWORD"), "")
        admin_email = _env_first(("ADMIN_EMAIL", "WORKSHOP_ADMIN_EMAIL"), "")
        return cls(
            app_name="AutoServe Pro — Workshop CRM & Service Planner",
            version="2.0.0",
            appearance_mode=_env_str("WORKSHOP_APPEARANCE", "dark"),
            database=DatabaseConfig(
                path=Path(_env_str("WORKSHOP_DB_PATH", str(DATABASE_DIR / "workshop.db"))),
                pool_size=max(1, _env_int("WORKSHOP_DB_POOL_SIZE", 5)),
            ),
            security=SecurityConfig(secret_key=_resolve_secret_key()),
            smtp=SMTPConfig(
                host=_env_str("WORKSHOP_SMTP_HOST", "smtp.gmail.com"),
                port=_env_int("WORKSHOP_SMTP_PORT", 587),
                username=smtp_user,
                # Google displays App Passwords in groups of four: "abcd efgh ijkl mnop".
                password=app_password.replace(" ", ""),
                sender=_env_str("WORKSHOP_SMTP_SENDER", smtp_user or "no-reply@autoserve.local"),
                use_ssl=_env_bool("WORKSHOP_SMTP_USE_SSL", False),
                force_mock=_env_bool("WORKSHOP_EMAIL_MOCK", False),
            ),
            sms=SMSConfig(mock_mode=_env_bool("WORKSHOP_SMS_MOCK", True)),
            admin=AdminSeed(
                full_name=_env_str("WORKSHOP_ADMIN_NAME", "Workshop Administrator"),
                username=_env_str("ADMIN_USERNAME", "admin"),
                password=_env_str("ADMIN_PASSWORD", DEFAULT_ADMIN_PASSWORD),
                email=(admin_email or DEFAULT_ADMIN_EMAIL).lower(),
                phone=_env_first(("ADMIN_PHONE", "WORKSHOP_ADMIN_PHONE"), "9999900000"),
                email_from_env=bool(admin_email),
            ),
        )


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    """Return the process-wide, lazily-built :class:`AppConfig` singleton."""
    return AppConfig.from_environment()
