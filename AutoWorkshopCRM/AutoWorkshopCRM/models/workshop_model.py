"""
models.workshop_model
=====================

Domain layer for the workshop: vehicles, mechanics, spare-part inventory,
service-job scheduling and billing.

Also contains the repairs catalogue (:class:`ServiceItem`), transactional
mechanic CRUD (:meth:`WorkshopModel.delete_mechanic` blocks or reassigns
in-progress work) and a deterministic 50+ vehicle demo-fleet generator.

The centrepiece is :meth:`WorkshopModel.allocate_parts`, an ACID unit of work
that — inside a single ``BEGIN IMMEDIATE`` transaction — verifies the job is
active, atomically decrements stock with a guarded ``UPDATE ... WHERE
stock_quantity >= ?``, records the job/part linkage, and recalculates the total
bill. Any failure (insufficient stock, missing part, constraint violation,
unexpected exception) rolls back *every* change made in the transaction.

All monetary arithmetic uses :class:`decimal.Decimal` quantised to two places.
This module is UI-agnostic and raises typed :class:`WorkshopError` subclasses.
"""

from __future__ import annotations

import logging
import random
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from core.config import BusinessRules
from core.db_manager import DatabaseManager, TransactionMode

logger = logging.getLogger(__name__)

CENT = Decimal("0.01")
PLATE_PATTERN = re.compile(r"^[A-Z0-9-]{4,15}$")
#: Strict national mobile format: exactly ten digits.
PHONE_PATTERN = re.compile(r"^[0-9]{10}$")
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #
class WorkshopError(Exception):
    """Base class for workshop domain errors (safe to show to users)."""


class ValidationError(WorkshopError):
    """Input failed a business validation rule."""


class NotFoundError(WorkshopError):
    """A referenced entity does not exist."""


class ConflictError(WorkshopError):
    """A uniqueness or referential constraint was violated."""


class InvalidStateError(WorkshopError):
    """The requested operation is not allowed in the entity's current state."""


class InsufficientStockError(WorkshopError):
    """Requested quantity exceeds the available inventory."""

    def __init__(self, part_name: str, requested: int, available: int) -> None:
        super().__init__(
            f"Insufficient stock for '{part_name}': requested {requested}, available {available}."
        )
        self.part_name = part_name
        self.requested = requested
        self.available = available


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def to_money(value: Any) -> Decimal:
    """Convert numbers/strings to a 2-dp :class:`Decimal` (banker-safe HALF_UP)."""
    try:
        return Decimal(str(value if value is not None else 0)).quantize(CENT, ROUND_HALF_UP)
    except (InvalidOperation, ValueError) as exc:
        raise ValidationError(f"'{value}' is not a valid monetary amount.") from exc


def normalize_plate(raw: str) -> str:
    """Upper-case and strip whitespace from a licence plate."""
    return re.sub(r"\s+", "", (raw or "")).upper()


def _parse_date(value: Any) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))


# --------------------------------------------------------------------------- #
# Entities / DTOs
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Vehicle:
    """Customer vehicle with owner contact details."""

    license_plate: str
    model: str
    owner_name: str
    owner_phone: str
    owner_email: Optional[str] = None


@dataclass(frozen=True)
class Mechanic:
    """Workshop technician."""

    mechanic_id: int
    name: str
    specialization: str
    status: str
    active_jobs: int = 0


@dataclass(frozen=True)
class MechanicDeletion:
    """Outcome of :meth:`WorkshopModel.delete_mechanic`."""

    mechanic_id: int
    name: str
    reassigned_jobs: int
    unassigned_jobs: int
    detached_history: int
    reassigned_to: Optional[int] = None


@dataclass(frozen=True)
class ServiceItem:
    """Repairs-catalogue entry (standard labour package)."""

    service_id: int
    service_name: str
    category: str
    base_labor_cost: Decimal
    estimated_hours: Decimal


@dataclass(frozen=True)
class SparePart:
    """Inventory line item."""

    part_id: int
    part_name: str
    stock_quantity: int
    unit_price: Decimal

    @property
    def stock_value(self) -> Decimal:
        """Extended value of on-hand stock."""
        return to_money(self.unit_price * self.stock_quantity)


@dataclass(frozen=True)
class ServiceJob:
    """Service job joined with vehicle and mechanic display data."""

    job_id: int
    license_plate: str
    vehicle_model: str
    owner_name: str
    mechanic_id: Optional[int]
    mechanic_name: Optional[str]
    description: str
    status: str
    labor_cost: Decimal
    total_bill: Decimal
    job_date: date
    service_id: Optional[int] = None
    service_name: Optional[str] = None


@dataclass(frozen=True)
class AllocationRequest:
    """A request to consume ``quantity`` units of ``part_id`` on a job."""

    part_id: int
    quantity: int


@dataclass(frozen=True)
class InvoiceLine:
    """One billed spare-part line."""

    part_id: int
    part_name: str
    quantity: int
    unit_price: Decimal

    @property
    def line_total(self) -> Decimal:
        """``quantity × unit_price`` rounded to cents."""
        return to_money(self.unit_price * self.quantity)


@dataclass(frozen=True)
class Invoice:
    """Billing aggregate produced by the job/vehicle/parts join."""

    job: ServiceJob
    owner_phone: str
    owner_email: Optional[str]
    lines: List[InvoiceLine] = field(default_factory=list)

    @property
    def parts_subtotal(self) -> Decimal:
        """Sum of all part line totals."""
        return to_money(sum((line.line_total for line in self.lines), Decimal("0")))

    @property
    def grand_total(self) -> Decimal:
        """Labour plus parts — mirrors ``Service_Jobs.total_bill``."""
        return to_money(self.job.labor_cost + self.parts_subtotal)


@dataclass(frozen=True)
class DashboardMetrics:
    """KPIs rendered on the overview page."""

    open_jobs: int
    completed_today: int
    revenue_month: Decimal
    available_mechanics: int
    total_mechanics: int
    low_stock_items: int
    inventory_value: Decimal
    registered_vehicles: int


# --------------------------------------------------------------------------- #
# Reference data
# --------------------------------------------------------------------------- #
#: (service_name, category, base labour ₹, estimated hours)
SERVICE_CATALOG: Tuple[Tuple[str, str, str, str], ...] = (
    ("Periodic Maintenance Service", "General", "1200.00", "2.0"),
    ("Brake Pad Replacement", "Brakes", "900.00", "1.5"),
    ("Wheel Alignment & Balancing", "Tyres", "800.00", "1.0"),
    ("AC Gas Recharge & Leak Test", "HVAC", "1500.00", "1.5"),
    ("Clutch Overhaul", "Transmission", "3500.00", "5.0"),
    ("Wiper & Coolant Top-up", "General", "600.00", "0.5"),
    # Advanced repairs
    ("Dual-Clutch Transmission Fluid Service", "Transmission", "4800.00", "3.0"),
    ("EV Battery Diagnostics", "EV Systems", "3200.00", "2.5"),
    ("Heads-Up Display Calibration", "Electronics & ADAS", "2800.00", "2.0"),
    ("ABS Actuator Module Refurbishment", "Brakes", "6500.00", "4.5"),
    ("Turbocharger Core Replacement", "Engine", "9500.00", "6.0"),
)

ADVANCED_SERVICES: Tuple[str, ...] = tuple(name for name, *_ in SERVICE_CATALOG[-5:])

_FLEET_MODELS: Tuple[str, ...] = (
    "Maruti Suzuki Baleno", "Maruti Suzuki Brezza", "Maruti Suzuki Dzire", "Maruti Suzuki Ertiga",
    "Maruti Suzuki Grand Vitara", "Hyundai i20", "Hyundai Venue", "Hyundai Verna",
    "Hyundai Ioniq 5", "Tata Punch", "Tata Harrier", "Tata Safari", "Tata Tiago EV",
    "Mahindra Scorpio-N", "Mahindra Thar", "Mahindra XUV 3XO", "Kia Seltos", "Kia Sonet",
    "Kia Carens", "Kia EV6", "Toyota Innova Hycross", "Toyota Fortuner", "Toyota Urban Cruiser Hyryder",
    "Honda Amaze", "Honda Elevate", "Skoda Slavia 1.5 TSI DSG", "Skoda Kodiaq",
    "Volkswagen Taigun GT DSG", "Volkswagen Virtus", "MG Hector", "MG ZS EV", "MG Comet EV",
    "Renault Kiger", "Nissan Magnite", "Citroen C3 Aircross", "Jeep Compass Trailhawk",
    "BMW X1 sDrive18d", "BMW 330Li M Sport", "Mercedes-Benz C 220d", "Mercedes-Benz GLA 200",
    "Audi A4 Premium Plus", "Audi Q5 45 TFSI", "Volvo XC40 Recharge", "BYD Atto 3",
)
_STATE_RTOS: Tuple[Tuple[str, int], ...] = (
    ("MH", 50), ("DL", 13), ("KA", 70), ("TN", 99), ("GJ", 38), ("TS", 38), ("KL", 86),
    ("UP", 96), ("RJ", 58), ("WB", 98), ("HR", 99), ("PB", 99), ("AP", 39), ("MP", 71),
)
_FIRST_NAMES: Tuple[str, ...] = (
    "Aarav", "Vivaan", "Aditya", "Ishaan", "Kabir", "Reyansh", "Arjun", "Sai", "Rahul", "Nikhil",
    "Ananya", "Diya", "Saanvi", "Ira", "Kavya", "Meera", "Pooja", "Sneha", "Riya", "Neha",
    "Farhan", "Zoya", "Gurpreet", "Harleen", "Joseph", "Maria", "Suresh", "Lakshmi", "Tenzin", "Ayesha",
)
_LAST_NAMES: Tuple[str, ...] = (
    "Sharma", "Verma", "Iyer", "Nair", "Reddy", "Patel", "Shah", "Gupta", "Mehta", "Joshi",
    "Kulkarni", "Deshpande", "Chatterjee", "Banerjee", "Das", "Singh", "Gill", "Khan", "Fernandes",
    "D'Souza", "Menon", "Pillai", "Rao", "Agarwal", "Bose", "Kapoor", "Malhotra", "Naidu", "Sethi", "Bhat",
)
_EMAIL_DOMAINS: Tuple[str, ...] = ("gmail.com", "outlook.com", "yahoo.in", "rediffmail.com", "icloud.com")


def generate_demo_fleet(count: int = 50, seed: int = 2026) -> List[Vehicle]:
    """
    Deterministically generate ``count`` realistic Indian-registered vehicles.

    Plates follow the ``SS00XX0000`` RTO format, phones are valid 10-digit
    mobile numbers (starting 6–9), and roughly three in four owners have an
    e-mail address. The same ``seed`` always yields the same fleet.
    """
    rng = random.Random(seed)
    fleet: List[Vehicle] = []
    plates: set[str] = set()
    phones: set[str] = set()
    while len(fleet) < count:
        state, max_rto = rng.choice(_STATE_RTOS)
        series = "".join(rng.choice("ABCDEFGHJKLMNPRSTUVWXYZ") for _ in range(2))
        plate = f"{state}{rng.randint(1, max_rto):02d}{series}{rng.randint(1, 9999):04d}"
        phone = f"{rng.choice('6789')}{rng.randint(0, 999_999_999):09d}"
        if plate in plates or phone in phones:
            continue
        plates.add(plate)
        phones.add(phone)
        first, last = rng.choice(_FIRST_NAMES), rng.choice(_LAST_NAMES)
        email = None
        if rng.random() < 0.75:
            handle = re.sub(r"[^a-z.]", "", f"{first}.{last}".lower())
            email = f"{handle}{rng.randint(1, 99)}@{rng.choice(_EMAIL_DOMAINS)}"
        fleet.append(Vehicle(plate, rng.choice(_FLEET_MODELS), f"{first} {last}", phone, email))
    return fleet


# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #
class WorkshopModel:
    """Repository + domain service for all workshop operations."""

    #: Allowed job status transitions (state machine).
    TRANSITIONS: Dict[str, Sequence[str]] = {
        "Pending": ("In Progress", "Cancelled"),
        "In Progress": ("Completed", "Cancelled"),
        "Completed": (),
        "Cancelled": (),
    }

    _JOB_SELECT = """
        SELECT j.job_id, j.license_plate, v.model AS vehicle_model, v.owner_name,
               j.mechanic_id, m.name AS mechanic_name, j.description, j.status,
               j.labor_cost, j.total_bill, j.job_date, j.service_id, s.service_name
          FROM Service_Jobs j
          JOIN Vehicles v        ON v.license_plate = j.license_plate
     LEFT JOIN Mechanics m       ON m.mechanic_id  = j.mechanic_id
     LEFT JOIN Service_Catalog s ON s.service_id   = j.service_id
    """

    def __init__(self, db: DatabaseManager, rules: BusinessRules) -> None:
        """
        :param db: Shared database manager.
        :param rules: Business constants (statuses, thresholds).
        """
        self._db = db
        self._rules = rules

    # ------------------------------------------------------------------ #
    # Row mappers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _to_job(row: sqlite3.Row) -> ServiceJob:
        return ServiceJob(
            job_id=row["job_id"], license_plate=row["license_plate"],
            vehicle_model=row["vehicle_model"], owner_name=row["owner_name"],
            mechanic_id=row["mechanic_id"], mechanic_name=row["mechanic_name"],
            description=row["description"], status=row["status"],
            labor_cost=to_money(row["labor_cost"]), total_bill=to_money(row["total_bill"]),
            job_date=_parse_date(row["job_date"]),
            service_id=row["service_id"], service_name=row["service_name"],
        )

    @staticmethod
    def _to_part(row: sqlite3.Row) -> SparePart:
        return SparePart(row["part_id"], row["part_name"], int(row["stock_quantity"]),
                         to_money(row["unit_price"]))

    # ------------------------------------------------------------------ #
    # Vehicles
    # ------------------------------------------------------------------ #
    def register_vehicle(self, vehicle: Vehicle) -> Vehicle:
        """Validate and persist a new vehicle; returns the normalised entity."""
        plate = normalize_plate(vehicle.license_plate)
        phone = (vehicle.owner_phone or "").strip()
        email = (vehicle.owner_email or "").strip().lower() or None
        if not PLATE_PATTERN.match(plate):
            raise ValidationError("Licence plate must be 4–15 letters, digits or dashes.")
        if not vehicle.model.strip() or not vehicle.owner_name.strip():
            raise ValidationError("Vehicle model and owner name are required.")
        if not PHONE_PATTERN.match(phone):
            raise ValidationError("Owner phone must be exactly 10 digits (numbers only).")
        if email and not EMAIL_PATTERN.match(email):
            raise ValidationError("Owner e-mail address is not valid.")
        clean = Vehicle(plate, vehicle.model.strip(), vehicle.owner_name.strip(), phone, email)
        try:
            self._db.execute(
                "INSERT INTO Vehicles (license_plate, model, owner_name, owner_phone, owner_email) "
                "VALUES (?, ?, ?, ?, ?);",
                (clean.license_plate, clean.model, clean.owner_name, clean.owner_phone,
                 clean.owner_email),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"Vehicle {plate} is already registered.") from exc
        return clean

    def list_vehicles(self, search: str = "") -> List[Vehicle]:
        """Return vehicles filtered by plate, model or owner (case-insensitive)."""
        like = f"%{search.strip()}%"
        rows = self._db.fetch_all(
            "SELECT license_plate, model, owner_name, owner_phone, owner_email FROM Vehicles "
            "WHERE license_plate LIKE ? OR model LIKE ? OR owner_name LIKE ? "
            "ORDER BY created_at DESC, license_plate;",
            (like, like, like),
        )
        return [Vehicle(**dict(row)) for row in rows]

    # ------------------------------------------------------------------ #
    # Mechanics
    # ------------------------------------------------------------------ #
    def add_mechanic(self, name: str, specialization: str) -> int:
        """Create a mechanic (status ``Available``) and return its id."""
        if not name.strip() or not specialization.strip():
            raise ValidationError("Mechanic name and specialization are required.")
        return self._db.execute_returning_id(
            "INSERT INTO Mechanics (name, specialization) VALUES (?, ?);",
            (name.strip(), specialization.strip()),
        )

    def list_mechanics(self) -> List[Mechanic]:
        """Return mechanics with their count of active jobs."""
        placeholders = ",".join("?" for _ in self._rules.active_job_statuses)
        rows = self._db.fetch_all(
            f"""SELECT m.mechanic_id, m.name, m.specialization, m.status,
                       COUNT(j.job_id) AS active_jobs
                  FROM Mechanics m
             LEFT JOIN Service_Jobs j
                    ON j.mechanic_id = m.mechanic_id AND j.status IN ({placeholders})
              GROUP BY m.mechanic_id
              ORDER BY m.name;""",
            tuple(self._rules.active_job_statuses),
        )
        return [Mechanic(**dict(row)) for row in rows]

    def get_mechanic(self, mechanic_id: int) -> Mechanic:
        """Read one mechanic with its active-job count."""
        mechanic = next((m for m in self.list_mechanics() if m.mechanic_id == mechanic_id), None)
        if mechanic is None:
            raise NotFoundError(f"Mechanic #{mechanic_id} not found.")
        return mechanic

    def update_mechanic(self, mechanic_id: int, name: str, specialization: str,
                        status: str) -> Mechanic:
        """
        Update a mechanic's profile and availability (transactional).

        :raises InvalidStateError: marking someone Off-Duty while they have
            a job In Progress.
        """
        if not name.strip() or not specialization.strip():
            raise ValidationError("Mechanic name and specialization are required.")
        if status not in self._rules.mechanic_statuses:
            raise ValidationError(f"Unknown mechanic status '{status}'.")
        with self._db.transaction(TransactionMode.IMMEDIATE) as conn:
            if conn.execute("SELECT 1 FROM Mechanics WHERE mechanic_id = ?;",
                            (mechanic_id,)).fetchone() is None:
                raise NotFoundError(f"Mechanic #{mechanic_id} not found.")
            in_progress = conn.execute(
                "SELECT COUNT(*) FROM Service_Jobs WHERE mechanic_id = ? AND status = 'In Progress';",
                (mechanic_id,)).fetchone()[0]
            if status == "Off-Duty" and in_progress:
                raise InvalidStateError(
                    f"Cannot mark Off-Duty: {in_progress} job(s) are In Progress. Reassign or finish them first.")
            conn.execute("UPDATE Mechanics SET name = ?, specialization = ?, status = ? "
                         "WHERE mechanic_id = ?;",
                         (name.strip(), specialization.strip(), status, mechanic_id))
        return self.get_mechanic(mechanic_id)

    def set_mechanic_status(self, mechanic_id: int, status: str) -> None:
        """Change only a mechanic's availability (same safety rules as :meth:`update_mechanic`)."""
        current = self.get_mechanic(mechanic_id)
        self.update_mechanic(mechanic_id, current.name, current.specialization, status)

    def delete_mechanic(self, mechanic_id: int, reassign_to: Optional[int] = None) -> MechanicDeletion:
        """
        Delete a mechanic without ever tripping a foreign-key failure.

        Runs in one ``BEGIN IMMEDIATE`` transaction:

        1. If the mechanic has jobs *In Progress* and no ``reassign_to`` is
           given, the deletion is **blocked** (:class:`ConflictError`).
        2. With ``reassign_to``, every active (Pending/In Progress) job card is
           moved to that mechanic, who becomes ``Busy``.
        3. Without it, Pending jobs are explicitly un-assigned.
        4. Completed/Cancelled history is detached (``mechanic_id = NULL``).
        5. The mechanic row is deleted. Any failure rolls back steps 2–5.
        """
        active = self._rules.active_job_statuses
        placeholders = ",".join("?" for _ in active)
        with self._db.transaction(TransactionMode.IMMEDIATE) as conn:
            row = conn.execute("SELECT name FROM Mechanics WHERE mechanic_id = ?;",
                               (mechanic_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"Mechanic #{mechanic_id} not found.")
            in_progress = conn.execute(
                "SELECT COUNT(*) FROM Service_Jobs WHERE mechanic_id = ? AND status = 'In Progress';",
                (mechanic_id,)).fetchone()[0]

            reassigned = unassigned = 0
            if reassign_to is not None:
                if reassign_to == mechanic_id:
                    raise ValidationError("Choose a different mechanic to take over the jobs.")
                target = conn.execute("SELECT status FROM Mechanics WHERE mechanic_id = ?;",
                                      (reassign_to,)).fetchone()
                if target is None:
                    raise NotFoundError(f"Mechanic #{reassign_to} not found.")
                if target["status"] == "Off-Duty":
                    raise InvalidStateError("Cannot reassign jobs to an off-duty mechanic.")
                reassigned = conn.execute(
                    f"UPDATE Service_Jobs SET mechanic_id = ? "
                    f"WHERE mechanic_id = ? AND status IN ({placeholders});",
                    (reassign_to, mechanic_id, *active)).rowcount
                if reassigned:
                    conn.execute("UPDATE Mechanics SET status = 'Busy' WHERE mechanic_id = ?;",
                                 (reassign_to,))
            elif in_progress:
                raise ConflictError(
                    f"{row['name']} has {in_progress} job card(s) In Progress. "
                    "Choose a mechanic to reassign them to before deleting.")
            else:
                unassigned = conn.execute(
                    "UPDATE Service_Jobs SET mechanic_id = NULL "
                    "WHERE mechanic_id = ? AND status = 'Pending';", (mechanic_id,)).rowcount

            detached = conn.execute("UPDATE Service_Jobs SET mechanic_id = NULL WHERE mechanic_id = ?;",
                                    (mechanic_id,)).rowcount
            conn.execute("DELETE FROM Mechanics WHERE mechanic_id = ?;", (mechanic_id,))
        logger.info("Deleted mechanic #%d (reassigned=%d, unassigned=%d, history=%d)",
                    mechanic_id, reassigned, unassigned, detached)
        return MechanicDeletion(mechanic_id, row["name"], reassigned, unassigned, detached, reassign_to)

    # ------------------------------------------------------------------ #
    # Spare parts
    # ------------------------------------------------------------------ #
    def add_part(self, part_name: str, stock_quantity: int, unit_price: Decimal) -> int:
        """Create an inventory item and return its id."""
        price = to_money(unit_price)
        if not part_name.strip():
            raise ValidationError("Part name is required.")
        if stock_quantity < 0 or price < 0:
            raise ValidationError("Stock and price must be non-negative.")
        try:
            return self._db.execute_returning_id(
                "INSERT INTO Spare_Parts (part_name, stock_quantity, unit_price) VALUES (?, ?, ?);",
                (part_name.strip(), stock_quantity, price),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"A part named '{part_name.strip()}' already exists.") from exc

    def restock_part(self, part_id: int, quantity: int) -> SparePart:
        """Atomically increase stock for ``part_id``."""
        if quantity <= 0:
            raise ValidationError("Restock quantity must be greater than zero.")
        with self._db.transaction() as conn:
            if conn.execute("UPDATE Spare_Parts SET stock_quantity = stock_quantity + ? "
                            "WHERE part_id = ?;", (quantity, part_id)).rowcount == 0:
                raise NotFoundError(f"Part #{part_id} not found.")
            row = conn.execute("SELECT * FROM Spare_Parts WHERE part_id = ?;", (part_id,)).fetchone()
        return self._to_part(row)

    def list_parts(self, search: str = "") -> List[SparePart]:
        """Return inventory sorted with the scarcest items first."""
        rows = self._db.fetch_all(
            "SELECT part_id, part_name, stock_quantity, unit_price FROM Spare_Parts "
            "WHERE part_name LIKE ? ORDER BY stock_quantity ASC, part_name;",
            (f"%{search.strip()}%",),
        )
        return [self._to_part(row) for row in rows]

    def low_stock_parts(self) -> List[SparePart]:
        """Parts at or below :attr:`BusinessRules.low_stock_threshold`."""
        rows = self._db.fetch_all(
            "SELECT part_id, part_name, stock_quantity, unit_price FROM Spare_Parts "
            "WHERE stock_quantity <= ? ORDER BY stock_quantity, part_name;",
            (self._rules.low_stock_threshold,),
        )
        return [self._to_part(row) for row in rows]

    # ------------------------------------------------------------------ #
    # Repairs catalogue
    # ------------------------------------------------------------------ #
    def seed_service_catalog(self) -> int:
        """Insert any missing catalogue services (idempotent); returns rows added."""
        return self._db.execute_many(
            "INSERT OR IGNORE INTO Service_Catalog "
            "(service_name, category, base_labor_cost, estimated_hours) VALUES (?, ?, ?, ?);",
            [(name, category, to_money(cost), str(Decimal(hours))) for name, category, cost, hours
             in SERVICE_CATALOG],
        )

    def list_services(self) -> List[ServiceItem]:
        """Active catalogue services grouped by category."""
        rows = self._db.fetch_all(
            "SELECT service_id, service_name, category, base_labor_cost, estimated_hours "
            "FROM Service_Catalog WHERE is_active = 1 ORDER BY category, service_name;")
        return [ServiceItem(r["service_id"], r["service_name"], r["category"],
                            to_money(r["base_labor_cost"]), Decimal(str(r["estimated_hours"])))
                for r in rows]

    def service_id_by_name(self, name: str) -> int:
        """Look up a catalogue id by exact (case-insensitive) name."""
        service_id = self._db.fetch_scalar(
            "SELECT service_id FROM Service_Catalog WHERE service_name = ?;", (name,))
        if service_id is None:
            raise NotFoundError(f"Service '{name}' is not in the catalogue.")
        return int(service_id)

    # ------------------------------------------------------------------ #
    # Service jobs & scheduling
    # ------------------------------------------------------------------ #
    def create_job(self, license_plate: str, mechanic_id: Optional[int], labor_cost: Decimal,
                   description: str = "", job_date: Optional[date] = None,
                   service_id: Optional[int] = None) -> int:
        """
        Open a new service job. Assigning a mechanic marks them ``Busy``.

        :param service_id: Optional repairs-catalogue entry the job is booked against.
        :raises NotFoundError: unknown vehicle, mechanic or service.
        :raises InvalidStateError: mechanic is off duty.
        :raises ValidationError: negative labour or a booking date in the past.
        """
        plate = normalize_plate(license_plate)
        labor = to_money(labor_cost)
        if labor < 0:
            raise ValidationError("Labour cost cannot be negative.")
        scheduled = job_date or date.today()
        if scheduled < date.today():
            raise ValidationError("Jobs cannot be booked on a past date.")
        with self._db.transaction(TransactionMode.IMMEDIATE) as conn:
            if conn.execute("SELECT 1 FROM Vehicles WHERE license_plate = ?;", (plate,)).fetchone() is None:
                raise NotFoundError(f"Vehicle {plate} is not registered.")
            if service_id is not None:
                service = conn.execute(
                    "SELECT service_name FROM Service_Catalog WHERE service_id = ? AND is_active = 1;",
                    (service_id,)).fetchone()
                if service is None:
                    raise NotFoundError(f"Catalogue service #{service_id} not found.")
                description = description.strip() or service["service_name"]
            if mechanic_id is not None:
                mech = conn.execute("SELECT status FROM Mechanics WHERE mechanic_id = ?;",
                                    (mechanic_id,)).fetchone()
                if mech is None:
                    raise NotFoundError(f"Mechanic #{mechanic_id} not found.")
                if mech["status"] == "Off-Duty":
                    raise InvalidStateError("Cannot assign a job to an off-duty mechanic.")
                conn.execute("UPDATE Mechanics SET status = 'Busy' WHERE mechanic_id = ?;",
                             (mechanic_id,))
            cursor = conn.execute(
                "INSERT INTO Service_Jobs (license_plate, mechanic_id, description, labor_cost, "
                "total_bill, job_date, service_id) VALUES (?, ?, ?, ?, ?, ?, ?);",
                (plate, mechanic_id, description.strip(), labor, labor, scheduled.isoformat(),
                 service_id),
            )
            if cursor.lastrowid is None:
                raise WorkshopError("Job insert did not return an id.")
            return int(cursor.lastrowid)

    def get_job(self, job_id: int) -> ServiceJob:
        """Fetch one job with its joined display data."""
        row = self._db.fetch_one(self._JOB_SELECT + " WHERE j.job_id = ?;", (job_id,))
        if row is None:
            raise NotFoundError(f"Job #{job_id} not found.")
        return self._to_job(row)

    def list_jobs(self, status: Optional[str] = None, search: str = "") -> List[ServiceJob]:
        """List jobs, newest first, optionally filtered by status or text."""
        clauses: List[str] = ["(j.license_plate LIKE ? OR v.owner_name LIKE ? OR v.model LIKE ?)"]
        params: List[Any] = []
        like = f"%{search.strip()}%"
        params.extend([like, like, like])
        if status:
            clauses.append("j.status = ?")
            params.append(status)
        rows = self._db.fetch_all(
            self._JOB_SELECT + " WHERE " + " AND ".join(clauses)
            + " ORDER BY j.job_date DESC, j.job_id DESC LIMIT 300;",
            tuple(params),
        )
        return [self._to_job(row) for row in rows]

    def schedule_board(self, day: date) -> Dict[str, List[ServiceJob]]:
        """
        Kanban data for ``day``: that day's jobs plus open jobs carried over
        from earlier dates, grouped by status (Cancelled excluded).
        """
        active = self._rules.active_job_statuses
        placeholders = ",".join("?" for _ in active)
        rows = self._db.fetch_all(
            self._JOB_SELECT
            + f" WHERE j.status <> 'Cancelled' AND (j.job_date = ? OR "
              f"(j.job_date < ? AND j.status IN ({placeholders})))"
              " ORDER BY j.job_date, j.job_id;",
            (day.isoformat(), day.isoformat(), *active),
        )
        board: Dict[str, List[ServiceJob]] = {"Pending": [], "In Progress": [], "Completed": []}
        for row in rows:
            job = self._to_job(row)
            board.setdefault(job.status, []).append(job)
        return board

    def update_job_status(self, job_id: int, new_status: str) -> ServiceJob:
        """
        Advance a job through its state machine (transactional).

        * ``In Progress`` → assigned mechanic becomes ``Busy``.
        * ``Cancelled`` → allocated parts are returned to stock and the bill is voided.
        * ``Completed``/``Cancelled`` → mechanic is freed if no other active jobs remain.
        """
        with self._db.transaction(TransactionMode.IMMEDIATE) as conn:
            row = conn.execute("SELECT status, mechanic_id FROM Service_Jobs WHERE job_id = ?;",
                               (job_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"Job #{job_id} not found.")
            current, mechanic_id = row["status"], row["mechanic_id"]
            if new_status not in self.TRANSITIONS.get(current, ()):
                raise InvalidStateError(f"Cannot move job #{job_id} from {current} to {new_status}.")

            if new_status == "Cancelled":
                conn.execute(
                    """UPDATE Spare_Parts
                          SET stock_quantity = stock_quantity + (
                              SELECT pu.quantity_used FROM Parts_Used pu
                               WHERE pu.job_id = ? AND pu.part_id = Spare_Parts.part_id)
                        WHERE part_id IN (SELECT part_id FROM Parts_Used WHERE job_id = ?);""",
                    (job_id, job_id),
                )
                conn.execute("DELETE FROM Parts_Used WHERE job_id = ?;", (job_id,))
                conn.execute("UPDATE Service_Jobs SET status = ?, total_bill = 0 WHERE job_id = ?;",
                             (new_status, job_id))
            else:
                conn.execute("UPDATE Service_Jobs SET status = ? WHERE job_id = ?;",
                             (new_status, job_id))

            if mechanic_id is not None:
                if new_status == "In Progress":
                    conn.execute("UPDATE Mechanics SET status = 'Busy' WHERE mechanic_id = ?;",
                                 (mechanic_id,))
                else:
                    self._release_mechanic_if_idle(conn, mechanic_id)
        return self.get_job(job_id)

    def _release_mechanic_if_idle(self, conn: sqlite3.Connection, mechanic_id: int) -> None:
        active = self._rules.active_job_statuses
        placeholders = ",".join("?" for _ in active)
        remaining = conn.execute(
            f"SELECT COUNT(*) FROM Service_Jobs WHERE mechanic_id = ? AND status IN ({placeholders});",
            (mechanic_id, *active),
        ).fetchone()[0]
        if remaining == 0:
            conn.execute("UPDATE Mechanics SET status = 'Available' "
                         "WHERE mechanic_id = ? AND status = 'Busy';", (mechanic_id,))

    # ------------------------------------------------------------------ #
    # THE ACID TEST — inventory allocation
    # ------------------------------------------------------------------ #
    def allocate_parts(self, job_id: int, requests: Iterable[AllocationRequest]) -> Invoice:
        """
        Atomically allocate spare parts to an active job and re-bill it.

        Steps executed inside one ``BEGIN IMMEDIATE`` transaction:

        1. Verify the job exists and is in an active state.
        2. For each part: guarded decrement
           ``UPDATE Spare_Parts SET stock_quantity = stock_quantity - :q
           WHERE part_id = :id AND stock_quantity >= :q`` — zero affected rows
           means another writer won the race or stock is insufficient.
        3. Upsert the ``Parts_Used`` linkage row, snapshotting the unit price.
        4. Promote a ``Pending`` job to ``In Progress``.
        5. Recompute ``total_bill = labor_cost + Σ(quantity × unit_price_at_use)``.

        Any exception at any step rolls back *all* of the above.

        :raises ValidationError: empty request or non-positive quantity.
        :raises NotFoundError: unknown job or part.
        :raises InvalidStateError: job is completed or cancelled.
        :raises InsufficientStockError: stock boundary would be crossed.
        """
        merged: Dict[int, int] = {}
        for req in requests:
            if not isinstance(req.quantity, int) or req.quantity <= 0:
                raise ValidationError("Each allocated quantity must be a positive whole number.")
            merged[req.part_id] = merged.get(req.part_id, 0) + req.quantity
        if not merged:
            raise ValidationError("Add at least one spare part to allocate.")

        with self._db.transaction(TransactionMode.IMMEDIATE) as conn:
            job = conn.execute("SELECT status FROM Service_Jobs WHERE job_id = ?;",
                               (job_id,)).fetchone()
            if job is None:
                raise NotFoundError(f"Job #{job_id} not found.")
            if job["status"] not in self._rules.active_job_statuses:
                raise InvalidStateError(
                    f"Job #{job_id} is {job['status']}; parts can only be added to active jobs.")

            for part_id, quantity in merged.items():
                part = conn.execute(
                    "SELECT part_name, stock_quantity, unit_price FROM Spare_Parts WHERE part_id = ?;",
                    (part_id,),
                ).fetchone()
                if part is None:
                    raise NotFoundError(f"Spare part #{part_id} not found.")

                decremented = conn.execute(
                    "UPDATE Spare_Parts SET stock_quantity = stock_quantity - ? "
                    "WHERE part_id = ? AND stock_quantity >= ?;",
                    (quantity, part_id, quantity),
                ).rowcount
                if decremented != 1:
                    raise InsufficientStockError(part["part_name"], quantity,
                                                 int(part["stock_quantity"]))

                conn.execute(
                    """INSERT INTO Parts_Used (job_id, part_id, quantity_used, unit_price_at_use)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT (job_id, part_id)
                       DO UPDATE SET quantity_used = quantity_used + excluded.quantity_used;""",
                    (job_id, part_id, quantity, to_money(part["unit_price"])),
                )

            if job["status"] == "Pending":
                conn.execute("UPDATE Service_Jobs SET status = 'In Progress' WHERE job_id = ?;",
                             (job_id,))
            self._recalculate_total(conn, job_id)
            invoice = self._build_invoice(conn, job_id)

        logger.info("Allocated %d part type(s) to job #%d; total=%s",
                    len(merged), job_id, invoice.grand_total)
        return invoice

    def _recalculate_total(self, conn: sqlite3.Connection, job_id: int) -> Decimal:
        """Recompute and persist ``total_bill`` using exact Decimal arithmetic."""
        labor = to_money(conn.execute("SELECT labor_cost FROM Service_Jobs WHERE job_id = ?;",
                                      (job_id,)).fetchone()[0])
        lines = conn.execute("SELECT quantity_used, unit_price_at_use FROM Parts_Used "
                             "WHERE job_id = ?;", (job_id,)).fetchall()
        total = to_money(labor + sum((to_money(r[1]) * r[0] for r in lines), Decimal("0")))
        conn.execute("UPDATE Service_Jobs SET total_bill = ? WHERE job_id = ?;", (total, job_id))
        return total

    # ------------------------------------------------------------------ #
    # Billing
    # ------------------------------------------------------------------ #
    def _build_invoice(self, conn: sqlite3.Connection, job_id: int) -> Invoice:
        header = conn.execute(
            self._JOB_SELECT.replace("v.owner_name", "v.owner_name, v.owner_phone, v.owner_email")
            + " WHERE j.job_id = ?;", (job_id,)
        ).fetchone()
        if header is None:
            raise NotFoundError(f"Job #{job_id} not found.")
        lines = conn.execute(
            """SELECT pu.part_id, sp.part_name, pu.quantity_used, pu.unit_price_at_use
                 FROM Parts_Used pu
                 JOIN Spare_Parts sp ON sp.part_id = pu.part_id
                WHERE pu.job_id = ?
             ORDER BY sp.part_name;""",
            (job_id,),
        ).fetchall()
        return Invoice(
            job=self._to_job(header),
            owner_phone=header["owner_phone"],
            owner_email=header["owner_email"],
            lines=[InvoiceLine(r["part_id"], r["part_name"], int(r["quantity_used"]),
                               to_money(r["unit_price_at_use"])) for r in lines],
        )

    def get_invoice(self, job_id: int) -> Invoice:
        """Return the billing join (job ⋈ vehicle ⋈ mechanic ⋈ parts) for ``job_id``."""
        with self._db.connection() as conn:
            return self._build_invoice(conn, job_id)

    # ------------------------------------------------------------------ #
    # Analytics
    # ------------------------------------------------------------------ #
    def dashboard_metrics(self) -> DashboardMetrics:
        """Aggregate KPIs in a single round-trip."""
        today = date.today()
        active = self._rules.active_job_statuses
        placeholders = ",".join("?" for _ in active)
        row = self._db.fetch_one(
            f"""SELECT
                  (SELECT COUNT(*) FROM Service_Jobs WHERE status IN ({placeholders})) AS open_jobs,
                  (SELECT COUNT(*) FROM Service_Jobs WHERE status = 'Completed' AND job_date = ?) AS done_today,
                  (SELECT COALESCE(SUM(total_bill), 0) FROM Service_Jobs
                    WHERE status = 'Completed' AND job_date >= ?) AS revenue_month,
                  (SELECT COUNT(*) FROM Mechanics WHERE status = 'Available') AS available,
                  (SELECT COUNT(*) FROM Mechanics) AS mechanics,
                  (SELECT COUNT(*) FROM Spare_Parts WHERE stock_quantity <= ?) AS low_stock,
                  (SELECT COALESCE(SUM(stock_quantity * unit_price), 0) FROM Spare_Parts) AS inv_value,
                  (SELECT COUNT(*) FROM Vehicles) AS vehicles;""",
            (*active, today.isoformat(), today.replace(day=1).isoformat(),
             self._rules.low_stock_threshold),
        )
        if row is None:
            raise WorkshopError("Metrics query returned no rows.")
        return DashboardMetrics(
            open_jobs=row["open_jobs"], completed_today=row["done_today"],
            revenue_month=to_money(row["revenue_month"]), available_mechanics=row["available"],
            total_mechanics=row["mechanics"], low_stock_items=row["low_stock"],
            inventory_value=to_money(row["inv_value"]), registered_vehicles=row["vehicles"],
        )

    # ------------------------------------------------------------------ #
    # Demo data
    # ------------------------------------------------------------------ #
    def seed_demo_data(self, fleet_size: int = 50) -> bool:
        """
        Populate demo data.

        * The repairs catalogue is always topped up (idempotent).
        * Empty database → full demo: 8 showcase vehicles + ``fleet_size``
          generated vehicles, mechanics, parts and jobs (incl. advanced repairs).
        * Existing database with fewer than ``fleet_size`` vehicles → the
          generated fleet is merged in (``INSERT OR IGNORE``) without touching
          existing records.

        :returns: ``True`` if anything was inserted.
        """
        added_services = self.seed_service_catalog()
        vehicle_count = int(self._db.fetch_scalar("SELECT COUNT(*) FROM Vehicles;", default=0))
        fleet = generate_demo_fleet(fleet_size)
        if vehicle_count:
            if vehicle_count >= fleet_size:
                return added_services > 0
            added = self._db.execute_many(
                "INSERT OR IGNORE INTO Vehicles (license_plate, model, owner_name, owner_phone, "
                "owner_email) VALUES (?, ?, ?, ?, ?);",
                [(v.license_plate, v.model, v.owner_name, v.owner_phone, v.owner_email) for v in fleet],
            )
            logger.info("Demo fleet topped up with %d vehicle(s).", added)
            return True

        showcase = (
            Vehicle("MH12AB1234", "Maruti Suzuki Swift", "Rohan Mehta", "9820011111", "rohan@example.com"),
            Vehicle("DL8CAF5030", "Hyundai Creta", "Anita Sharma", "9810022222", "anita@example.com"),
            Vehicle("KA03MN7788", "Tata Nexon EV", "Vikram Rao", "9845033333", None),
            Vehicle("TN09BX4521", "Honda City", "Priya Iyer", "9884044444", "priya@example.com"),
            Vehicle("GJ01KL9090", "Mahindra XUV700", "Karan Patel", "9825055555", None),
            Vehicle("KA05MQ2211", "Volkswagen Virtus GT DSG", "Sneha Kulkarni", "9900066666",
                    "sneha.k@example.com"),
            Vehicle("MH02FZ7007", "BMW 3 Series Gran Limousine", "Aditya Malhotra", "9769077777",
                    "aditya.m@example.com"),
            Vehicle("HR26DK4545", "Skoda Kushaq 1.5 TSI", "Gurpreet Gill", "9811088888", None),
        )
        for vehicle in (*showcase, *fleet):
            self.register_vehicle(vehicle)
        mechanics = [self.add_mechanic(n, s) for n, s in (
            ("Arjun Singh", "Engine & Transmission"), ("Meera Nair", "Electrical & EV Systems"),
            ("Sanjay Kulkarni", "Suspension & Brakes"), ("Fatima Khan", "Body & Paint"),
            ("Rohit Deshmukh", "ADAS & Electronics"))]
        parts = [self.add_part(n, q, Decimal(p)) for n, q, p in (
            ("Engine Oil 5W-30 (1L)", 40, "650.00"), ("Oil Filter", 25, "320.00"),
            ("Brake Pad Set (Front)", 12, "2450.00"), ("Air Filter", 18, "540.00"),
            ("Spark Plug (Iridium)", 30, "780.00"), ("Coolant (1L)", 4, "410.00"),
            ("Wiper Blade Pair", 3, "899.00"), ("12V Battery 45Ah", 6, "5600.00"),
            ("DSG Transmission Fluid (1L)", 24, "1150.00"), ("DSG Fluid Filter", 6, "1890.00"),
            ("ABS Modulator Seal Kit", 5, "3400.00"), ("Turbocharger CHRA Core", 2, "38500.00"),
            ("HUD Calibration Target Kit", 3, "1200.00"))]
        svc = self.service_id_by_name
        today = date.today()
        j1 = self.create_job("MH12AB1234", mechanics[0], Decimal("1200"), "", today,
                             svc("Periodic Maintenance Service"))
        self.allocate_parts(j1, [AllocationRequest(parts[0], 4), AllocationRequest(parts[1], 1)])
        j2 = self.create_job("DL8CAF5030", mechanics[2], Decimal("900"), "Front brake noise", today,
                             svc("Brake Pad Replacement"))
        self.allocate_parts(j2, [AllocationRequest(parts[2], 1)])
        self.update_job_status(j2, "Completed")
        self.create_job("KA03MN7788", mechanics[1], Decimal("3200"), "", today,
                        svc("EV Battery Diagnostics"))
        j4 = self.create_job("KA05MQ2211", mechanics[0], Decimal("4800"), "", today,
                             svc("Dual-Clutch Transmission Fluid Service"))
        self.allocate_parts(j4, [AllocationRequest(parts[8], 6), AllocationRequest(parts[9], 1)])
        self.create_job("MH02FZ7007", mechanics[4], Decimal("2800"), "", today + timedelta(days=1),
                        svc("Heads-Up Display Calibration"))
        self.create_job("HR26DK4545", None, Decimal("6500"), "Pulsating brake pedal, ABS warning lamp",
                        today + timedelta(days=2), svc("ABS Actuator Module Refurbishment"))
        self.create_job("GJ01KL9090", None, Decimal("9500"), "Low boost, blue smoke under load",
                        today + timedelta(days=3), svc("Turbocharger Core Replacement"))
        self.create_job("TN09BX4521", None, Decimal("600"), "", today + timedelta(days=1),
                        svc("Wiper & Coolant Top-up"))
        logger.info("Demo data seeded (%d vehicles).", len(showcase) + len(fleet))
        return True
