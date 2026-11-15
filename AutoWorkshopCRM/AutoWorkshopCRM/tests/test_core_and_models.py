"""
Headless test-suite (no display required): ``python -m unittest discover -s tests -v``

Covers the connection pool / transaction wrapper and migrations, bcrypt +
OTP two-step authentication, OTP-gated credential changes, phone validation,
demo-data scaling, mechanic CRUD safety and the ACID inventory allocation
(including a concurrent-writer race).
"""

from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import List, Optional, Tuple

from core.config import get_config
from core.db_manager import DatabaseManager
from core.notification_service import DispatchReceipt
from models.auth_model import (
    AccountLockedError,
    AuthModel,
    CredentialChange,
    CredentialValidationError,
    InvalidCredentialsError,
    OTPChannel,
    OTPCooldownError,
    OTPDeliveryError,
    OTPPurpose,
    OTPVerificationStatus,
)
from models.workshop_model import (
    ADVANCED_SERVICES,
    AllocationRequest,
    ConflictError,
    InsufficientStockError,
    InvalidStateError,
    ValidationError,
    Vehicle,
    WorkshopModel,
    generate_demo_fleet,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #
class _CapturingEmail:
    """Synchronous e-mail transport that records messages instead of sending."""

    is_mock = True

    def __init__(self) -> None:
        self.sent: List[Tuple[str, str, str]] = []
        self.fail = False

    def send(self, recipient: str, subject: str, body_text: str,
             body_html: Optional[str] = None) -> DispatchReceipt:
        self.sent.append((recipient, subject, body_text))
        return DispatchReceipt("email", recipient, not self.fail, "Test", "<id>", 0,
                               "SMTPAuthenticationError: bad app password" if self.fail else "ok")


class _CapturingSMS:
    is_mock = True

    def __init__(self) -> None:
        self.sent: List[Tuple[str, str]] = []

    def send(self, phone: str, body: str) -> DispatchReceipt:
        self.sent.append((phone, body))
        return DispatchReceipt("sms", phone, True, "Test", "SM1", 0, "ok")


class _FakeNotifier:
    def __init__(self) -> None:
        self.email = _CapturingEmail()
        self.sms = _CapturingSMS()


def _last_code(text: str) -> str:
    return next(tok for tok in text.replace(".", " ").split() if tok.isdigit() and len(tok) == 6)


class _DBTestCase(unittest.TestCase):
    """Fresh on-disk database per test."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        cfg = get_config()
        self.config = cfg
        self.db_path = Path(self._tmp.name) / "test.db"
        self.db = DatabaseManager(replace(cfg.database, path=self.db_path))
        self.db.initialize_schema()

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #
class DatabaseManagerTests(_DBTestCase):
    def test_schema_version_and_health(self) -> None:
        self.assertEqual(self.db.schema_version(), 2)
        self.assertTrue(self.db.health_check())

    def test_transaction_rolls_back_on_exception(self) -> None:
        with self.assertRaises(RuntimeError):
            with self.db.transaction() as conn:
                conn.execute("INSERT INTO Mechanics (name, specialization) VALUES ('A', 'B');")
                raise RuntimeError("boom")
        self.assertEqual(self.db.fetch_scalar("SELECT COUNT(*) FROM Mechanics;"), 0)

    def test_check_constraints(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT INTO Spare_Parts (part_name, stock_quantity, unit_price) "
                            "VALUES ('X', -1, 10);")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT INTO Vehicles (license_plate, model, owner_name, owner_phone) "
                            "VALUES ('MH01AA0001', 'M', 'O', '98765abcde');")

    def test_foreign_keys_enforced(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT INTO Service_Jobs (license_plate) VALUES ('NOPE123');")


class MigrationTests(unittest.TestCase):
    def test_v1_database_is_upgraded_and_admin_backfilled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy.db"
            legacy = sqlite3.connect(path)
            legacy.executescript((FIXTURES / "schema_v1.sql").read_text())
            legacy.execute("INSERT INTO Users (full_name, email, phone, role) "
                           "VALUES ('Old Admin', 'admin@autoserve.local', '+919999900000', 'Admin');")
            legacy.execute("INSERT INTO Vehicles VALUES ('MH12AB1234', 'Swift', 'R', '+919820011111', "
                           "NULL, datetime('now'));")
            legacy.commit()
            legacy.close()

            cfg = get_config()
            db = DatabaseManager(replace(cfg.database, path=path))
            try:
                db.initialize_schema()
                self.assertEqual(db.schema_version(), 2)
                self.assertEqual(db.fetch_scalar("SELECT owner_phone FROM Vehicles;"), "9820011111")
                auth = AuthModel(db, replace(cfg.security, bcrypt_rounds=4), _FakeNotifier())  # type: ignore[arg-type]
                auth.ensure_admin(cfg.admin)
                row = db.fetch_one("SELECT username, password_hash, phone FROM Users;")
                self.assertEqual(row["username"], cfg.admin.username)
                self.assertTrue(row["password_hash"].startswith("$2"))
                self.assertEqual(row["phone"], "9999900000")
                self.assertEqual(auth.authenticate(cfg.admin.username, cfg.admin.password).role, "Admin")
                auth.shutdown()
            finally:
                db.close()


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #
class AuthTests(_DBTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.now = 1000.0
        self.notifier = _FakeNotifier()
        self.security = replace(self.config.security, bcrypt_rounds=4)
        self.auth = AuthModel(self.db, self.security, self.notifier,  # type: ignore[arg-type]
                              clock=lambda: self.now)
        self.auth.ensure_admin(self.config.admin)
        self.username = self.config.admin.username
        self.password = self.config.admin.password

    def tearDown(self) -> None:
        self.auth.shutdown()
        super().tearDown()

    def _login_code(self, channel: OTPChannel = OTPChannel.EMAIL) -> Tuple[int, str]:
        challenge = self.auth.start_login(self.username, self.password, channel).result(5)
        sent = self.notifier.email.sent if channel is OTPChannel.EMAIL else self.notifier.sms.sent
        return challenge.user_id, _last_code(sent[-1][-1])

    def test_password_is_stored_as_bcrypt_hash(self) -> None:
        stored = self.db.fetch_scalar("SELECT password_hash FROM Users;")
        self.assertTrue(stored.startswith("$2b$"))
        self.assertNotIn(self.password, stored)

    def test_two_step_login_creates_session(self) -> None:
        user_id, code = self._login_code()
        status, session = self.auth.verify_login_otp(user_id, code)
        self.assertIs(status, OTPVerificationStatus.VERIFIED)
        self.assertIsNotNone(session and self.auth.sessions.validate(session.token))
        self.assertIs(self.auth.verify_login_otp(user_id, code)[0], OTPVerificationStatus.EXPIRED)

    def test_sms_channel_uses_10_digit_phone(self) -> None:
        self._login_code(OTPChannel.SMS)
        self.assertEqual(self.notifier.sms.sent[-1][0], "9999900000")

    def test_wrong_password_and_lockout(self) -> None:
        for _ in range(self.security.login_max_failures):
            with self.assertRaises(InvalidCredentialsError):
                self.auth.start_login(self.username, "wrong-pass1", OTPChannel.EMAIL).result(5)
        with self.assertRaises(AccountLockedError):
            self.auth.start_login(self.username, self.password, OTPChannel.EMAIL).result(5)
        self.now += self.security.login_lockout_seconds + 1
        self.auth.start_login(self.username, self.password, OTPChannel.EMAIL).result(5)
        self.assertEqual(len(self.notifier.email.sent), 1)

    def test_sql_injection_username_is_rejected(self) -> None:
        with self.assertRaises(InvalidCredentialsError):
            self.auth.authenticate("admin' OR '1'='1", "x")
        with self.assertRaises(InvalidCredentialsError):
            self.auth.authenticate("admin", "' OR '1'='1")

    def test_otp_expires_within_five_minutes(self) -> None:
        user_id, code = self._login_code()
        self.assertLessEqual(self.auth.remaining_seconds(OTPPurpose.LOGIN, user_id), 300)
        self.now += 300
        self.assertIs(self.auth.verify_login_otp(user_id, code)[0], OTPVerificationStatus.EXPIRED)

    def test_otp_lockout_and_cooldown(self) -> None:
        user_id, code = self._login_code()
        wrong = "000000" if code != "000000" else "111111"
        statuses = [self.auth.verify_login_otp(user_id, wrong)[0]
                    for _ in range(self.security.otp_max_attempts)]
        self.assertIs(statuses[-1], OTPVerificationStatus.LOCKED)
        self.assertIs(self.auth.verify_login_otp(user_id, code)[0], OTPVerificationStatus.NOT_FOUND)
        self.auth.resend_login_otp(user_id, OTPChannel.EMAIL).result(5)  # locked code was discarded
        with self.assertRaises(OTPCooldownError):
            self.auth.resend_login_otp(user_id, OTPChannel.EMAIL).result(5)
        self.now += self.security.otp_resend_cooldown_seconds
        self.auth.resend_login_otp(user_id, OTPChannel.EMAIL).result(5)

    def test_plain_code_never_stored(self) -> None:
        user_id, code = self._login_code()
        record = self.auth.otp_cache._records[self.auth.otp_key(OTPPurpose.LOGIN, user_id)]
        self.assertNotIn(code.encode(), record.code_digest)

    def test_delivery_failure_surfaces_and_discards_code(self) -> None:
        self.notifier.email.fail = True
        with self.assertRaises(OTPDeliveryError):
            self.auth.start_login(self.username, self.password, OTPChannel.EMAIL).result(5)
        self.notifier.email.fail = False
        self.auth.start_login(self.username, self.password, OTPChannel.EMAIL).result(5)

    def test_credential_change_requires_otp_before_update(self) -> None:
        user_id, code = self._login_code()
        _, session = self.auth.verify_login_otp(user_id, code)
        assert session is not None
        change = CredentialChange("boss", "boss@gmail.com", "9123456789", "NewPass123", "NewPass123")
        self.now += 31
        challenge = self.auth.request_credential_change(user_id, self.password, change).result(5)
        recipient, subject, body = self.notifier.email.sent[-1]
        self.assertEqual(recipient, self.config.admin.email)  # current address, not the new one
        self.assertIn("confirm", subject.lower())
        self.assertEqual(self.db.fetch_scalar("SELECT username FROM Users;"), self.username)
        self.assertIs(self.auth.confirm_credential_change(user_id, "999999" if _last_code(body) != "999999"
                                                          else "888888")[0],
                      OTPVerificationStatus.INVALID)
        status, new_session = self.auth.confirm_credential_change(user_id, _last_code(body))
        self.assertIs(status, OTPVerificationStatus.VERIFIED)
        self.assertEqual(challenge.purpose, OTPPurpose.CREDENTIAL_CHANGE)
        self.assertIsNone(self.auth.sessions.validate(session.token))
        assert new_session is not None
        self.assertEqual(new_session.user.username, "boss")
        self.assertEqual(self.auth.authenticate("boss", "NewPass123").email, "boss@gmail.com")
        with self.assertRaises(InvalidCredentialsError):
            self.auth.authenticate("boss", self.password)

    def test_credential_change_validation(self) -> None:
        cases = [
            CredentialChange("ab", "a@b.co"),
            CredentialChange("valid_name", "not-an-email"),
            CredentialChange("valid_name", "a@b.co", "12345"),
            CredentialChange("valid_name", "a@b.co", None, "short1", "short1"),
            CredentialChange("valid_name", "a@b.co", None, "LongEnough1", "Mismatch1"),
        ]
        for change in cases:
            with self.subTest(change=change), self.assertRaises(CredentialValidationError):
                self.auth.request_credential_change(1, self.password, change).result(5)
        with self.assertRaises(InvalidCredentialsError):
            self.auth.request_credential_change(1, "bad-password1", cases[0]).result(5)

    def test_login_code_cannot_authorise_credential_change(self) -> None:
        user_id, code = self._login_code()
        self.assertIs(self.auth.confirm_credential_change(user_id, code)[0],
                      OTPVerificationStatus.NOT_FOUND)


# --------------------------------------------------------------------------- #
# Workshop domain
# --------------------------------------------------------------------------- #
class _WorkshopCase(_DBTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.model = WorkshopModel(self.db, self.config.business)
        self.model.seed_service_catalog()
        self.model.register_vehicle(Vehicle("MH01AA0001", "Swift", "Test Owner", "9000000001"))


class SeedAndValidationTests(_WorkshopCase):
    def test_phone_must_be_exactly_10_digits(self) -> None:
        for bad in ("12345", "98765432101", "+919876543210", "98765-43210", "98765abcde"):
            with self.subTest(phone=bad), self.assertRaises(ValidationError):
                self.model.register_vehicle(Vehicle("KA01AB0002", "City", "X", bad))

    def test_demo_fleet_is_realistic_and_deterministic(self) -> None:
        fleet = generate_demo_fleet(50)
        self.assertEqual(fleet, generate_demo_fleet(50))
        self.assertEqual(len({v.license_plate for v in fleet}), 50)
        self.assertTrue(all(len(v.owner_phone) == 10 and v.owner_phone.isdigit() for v in fleet))
        self.assertGreater(len({v.model.split()[0] for v in fleet}), 8)

    def test_seed_creates_50_plus_vehicles_and_advanced_services(self) -> None:
        self.db.execute("DELETE FROM Vehicles;")
        self.assertTrue(self.model.seed_demo_data())
        self.assertGreaterEqual(len(self.model.list_vehicles()), 50)
        names = {s.service_name for s in self.model.list_services()}
        self.assertTrue(set(ADVANCED_SERVICES) <= names)
        self.assertEqual(len(ADVANCED_SERVICES), 5)
        self.assertTrue(any(j.service_name == "Turbocharger Core Replacement"
                            for j in self.model.list_jobs()))

    def test_existing_database_is_topped_up(self) -> None:
        self.model.seed_demo_data()
        self.assertGreaterEqual(len(self.model.list_vehicles()), 50)
        self.assertTrue(any(v.license_plate == "MH01AA0001" for v in self.model.list_vehicles()))

    def test_job_booking_rules(self) -> None:
        sid = self.model.service_id_by_name("EV Battery Diagnostics")
        job = self.model.create_job("MH01AA0001", None, Decimal("3200"), "", date.today(), sid)
        self.assertEqual(self.model.get_job(job).service_name, "EV Battery Diagnostics")
        with self.assertRaises(ValidationError):
            self.model.create_job("MH01AA0001", None, Decimal("0"), "", date.today() - timedelta(days=1))


class MechanicCrudTests(_WorkshopCase):
    def setUp(self) -> None:
        super().setUp()
        self.a = self.model.add_mechanic("Asha", "Engine")
        self.b = self.model.add_mechanic("Bilal", "Brakes")

    def test_update_mechanic(self) -> None:
        m = self.model.update_mechanic(self.a, "Asha R", "EV Systems", "Off-Duty")
        self.assertEqual((m.name, m.specialization, m.status), ("Asha R", "EV Systems", "Off-Duty"))

    def test_cannot_go_off_duty_with_in_progress_job(self) -> None:
        job = self.model.create_job("MH01AA0001", self.a, Decimal("100"))
        self.model.update_job_status(job, "In Progress")
        with self.assertRaises(InvalidStateError):
            self.model.update_mechanic(self.a, "Asha", "Engine", "Off-Duty")

    def test_delete_blocked_when_in_progress(self) -> None:
        job = self.model.create_job("MH01AA0001", self.a, Decimal("100"))
        self.model.update_job_status(job, "In Progress")
        with self.assertRaises(ConflictError):
            self.model.delete_mechanic(self.a)
        self.assertEqual(self.model.get_job(job).mechanic_id, self.a)  # nothing changed

    def test_delete_with_reassignment(self) -> None:
        active = self.model.create_job("MH01AA0001", self.a, Decimal("100"))
        self.model.update_job_status(active, "In Progress")
        done = self.model.create_job("MH01AA0001", self.a, Decimal("100"))
        self.model.update_job_status(done, "In Progress")
        self.model.update_job_status(done, "Completed")
        report = self.model.delete_mechanic(self.a, reassign_to=self.b)
        self.assertEqual((report.reassigned_jobs, report.detached_history), (1, 1))
        self.assertEqual(self.model.get_job(active).mechanic_id, self.b)
        self.assertIsNone(self.model.get_job(done).mechanic_id)
        self.assertEqual(self.model.get_mechanic(self.b).status, "Busy")
        self.assertEqual(len(self.model.list_mechanics()), 1)

    def test_delete_unassigns_pending_jobs(self) -> None:
        pending = self.model.create_job("MH01AA0001", self.a, Decimal("100"))
        report = self.model.delete_mechanic(self.a)
        self.assertEqual(report.unassigned_jobs, 1)
        self.assertIsNone(self.model.get_job(pending).mechanic_id)


class InventoryACIDTests(_WorkshopCase):
    def setUp(self) -> None:
        super().setUp()
        self.mech = self.model.add_mechanic("M", "Engine")
        self.oil = self.model.add_part("Oil", 10, Decimal("100.50"))
        self.pad = self.model.add_part("Pads", 1, Decimal("2000"))
        self.job = self.model.create_job("MH01AA0001", self.mech, Decimal("500"))

    def _stock(self, part_id: int) -> int:
        return self.db.fetch_scalar("SELECT stock_quantity FROM Spare_Parts WHERE part_id = ?;", (part_id,))

    def test_successful_allocation_updates_stock_and_bill(self) -> None:
        invoice = self.model.allocate_parts(self.job, [AllocationRequest(self.oil, 3)])
        self.assertEqual(self._stock(self.oil), 7)
        self.assertEqual(invoice.grand_total, Decimal("801.50"))
        self.assertEqual(self.model.get_job(self.job).total_bill, Decimal("801.50"))
        self.assertEqual(self.model.get_job(self.job).status, "In Progress")

    def test_total_rollback_when_any_line_fails(self) -> None:
        with self.assertRaises(InsufficientStockError):
            self.model.allocate_parts(self.job, [AllocationRequest(self.oil, 4),
                                                 AllocationRequest(self.pad, 2)])
        self.assertEqual(self._stock(self.oil), 10)
        self.assertEqual(self.db.fetch_scalar("SELECT COUNT(*) FROM Parts_Used;"), 0)
        job = self.model.get_job(self.job)
        self.assertEqual((job.status, job.total_bill), ("Pending", Decimal("500.00")))

    def test_cannot_allocate_to_completed_job(self) -> None:
        self.model.update_job_status(self.job, "In Progress")
        self.model.update_job_status(self.job, "Completed")
        with self.assertRaises(InvalidStateError):
            self.model.allocate_parts(self.job, [AllocationRequest(self.oil, 1)])

    def test_cancel_returns_parts_to_stock(self) -> None:
        self.model.allocate_parts(self.job, [AllocationRequest(self.oil, 5)])
        self.model.update_job_status(self.job, "Cancelled")
        self.assertEqual(self._stock(self.oil), 10)
        self.assertEqual(self.model.get_job(self.job).total_bill, Decimal("0.00"))

    def test_concurrent_allocations_never_oversell(self) -> None:
        jobs = [self.model.create_job("MH01AA0001", None, Decimal("0")) for _ in range(8)]
        results: list = []
        barrier = threading.Barrier(len(jobs))

        def worker(job_id: int) -> None:
            barrier.wait()
            try:
                self.model.allocate_parts(job_id, [AllocationRequest(self.pad, 1)])
                results.append("ok")
            except InsufficientStockError:
                results.append("rejected")

        threads = [threading.Thread(target=worker, args=(j,)) for j in jobs]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results.count("ok"), 1)
        self.assertEqual(self._stock(self.pad), 0)


if __name__ == "__main__":
    unittest.main()
