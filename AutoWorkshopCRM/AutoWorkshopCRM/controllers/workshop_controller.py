"""
controllers.workshop_controller
===============================

Thin application-service layer between the dashboard view and
:class:`~models.workshop_model.WorkshopModel`.

Responsibilities:

* Parse raw widget strings into typed values (``Decimal``, ``int``, ``date``).
* Delegate to the model.
* Convert every outcome into an :class:`OperationResult` for the view.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional, Sequence, Tuple

from controllers.base_controller import BaseController, OperationResult
from core.config import BusinessRules
from models.workshop_model import (
    AllocationRequest,
    DashboardMetrics,
    Invoice,
    Mechanic,
    MechanicDeletion,
    ServiceItem,
    ServiceJob,
    SparePart,
    ValidationError,
    Vehicle,
    WorkshopModel,
)


class WorkshopController(BaseController):
    """Use-case façade consumed by :mod:`views.dashboard_view`."""

    def __init__(self, model: WorkshopModel, rules: BusinessRules) -> None:
        """
        :param model: Workshop domain model.
        :param rules: Business constants (exposed to views for option lists).
        """
        self._model = model
        self.rules = rules

    # ------------------------------------------------------------------ #
    # Parsing helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _money(raw: str, label: str) -> Decimal:
        try:
            value = Decimal((raw or "0").replace(",", "").strip() or "0")
        except InvalidOperation as exc:
            raise ValidationError(f"{label} must be a number.") from exc
        if value < 0:
            raise ValidationError(f"{label} cannot be negative.")
        return value

    @staticmethod
    def _int(raw: str, label: str, minimum: int = 0) -> int:
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"{label} must be a whole number.") from exc
        if value < minimum:
            raise ValidationError(f"{label} must be at least {minimum}.")
        return value

    def format_money(self, amount: Decimal) -> str:
        """Render an amount with the configured currency symbol."""
        return f"{self.rules.currency_symbol}{amount:,.2f}"

    # ------------------------------------------------------------------ #
    # Queries
    # ------------------------------------------------------------------ #
    def dashboard_metrics(self) -> OperationResult[DashboardMetrics]:
        """KPIs for the overview page."""
        return self._run(self._model.dashboard_metrics)

    def vehicles(self, search: str = "") -> OperationResult[List[Vehicle]]:
        """Vehicles matching ``search``."""
        return self._run(lambda: self._model.list_vehicles(search))

    def mechanics(self) -> OperationResult[List[Mechanic]]:
        """All mechanics with active-job counts."""
        return self._run(self._model.list_mechanics)

    def parts(self, search: str = "") -> OperationResult[List[SparePart]]:
        """Inventory matching ``search``."""
        return self._run(lambda: self._model.list_parts(search))

    def low_stock(self) -> OperationResult[List[SparePart]]:
        """Parts at or below the low-stock threshold."""
        return self._run(self._model.low_stock_parts)

    def jobs(self, status: Optional[str] = None, search: str = "") -> OperationResult[List[ServiceJob]]:
        """Jobs filtered by status (``None``/"All" = every status) and text."""
        status = None if status in (None, "", "All") else status
        return self._run(lambda: self._model.list_jobs(status, search))

    def schedule(self, day: date) -> OperationResult[Tuple[date, Dict[str, List[ServiceJob]]]]:
        """Kanban board for ``day`` (chosen with the date picker)."""
        return self._run(lambda: (day, self._model.schedule_board(day)))

    def services(self) -> OperationResult[List[ServiceItem]]:
        """Active repairs-catalogue entries."""
        return self._run(self._model.list_services)

    def invoice(self, job_id: int) -> OperationResult[Invoice]:
        """Billing join for one job."""
        return self._run(lambda: self._model.get_invoice(job_id))

    # ------------------------------------------------------------------ #
    # Commands
    # ------------------------------------------------------------------ #
    def register_vehicle(self, plate: str, model: str, owner: str, phone: str,
                         email: str) -> OperationResult[Vehicle]:
        """Register a customer vehicle."""
        return self._run(lambda: self._model.register_vehicle(
            Vehicle(plate, model, owner, phone, email or None)), "Vehicle registered.")

    def add_mechanic(self, name: str, specialization: str) -> OperationResult[int]:
        """Hire a mechanic."""
        return self._run(lambda: self._model.add_mechanic(name, specialization), "Mechanic added.")

    def update_mechanic(self, mechanic_id: Optional[int], name: str, specialization: str,
                        status: str) -> OperationResult[Mechanic]:
        """Edit a mechanic's name, specialization and availability."""
        def _update() -> Mechanic:
            if mechanic_id is None:
                raise ValidationError("Select a mechanic first.")
            return self._model.update_mechanic(mechanic_id, name, specialization, status)
        return self._run(_update, "Mechanic updated.")

    def set_mechanic_status(self, mechanic_id: Optional[int], status: str) -> OperationResult[None]:
        """Override a mechanic's availability."""
        def _set() -> None:
            if mechanic_id is None:
                raise ValidationError("Select a mechanic first.")
            self._model.set_mechanic_status(mechanic_id, status)
        return self._run(_set, f"Mechanic marked {status}.")

    def delete_mechanic(self, mechanic_id: Optional[int],
                        reassign_to: Optional[int] = None) -> OperationResult[MechanicDeletion]:
        """Transactionally delete a mechanic (blocked or reassigned if work is In Progress)."""
        def _delete() -> MechanicDeletion:
            if mechanic_id is None:
                raise ValidationError("Select a mechanic first.")
            return self._model.delete_mechanic(mechanic_id, reassign_to)
        result = self._run(_delete)
        if not result.success or result.data is None:
            return result
        report = result.data
        details = []
        if report.reassigned_jobs:
            details.append(f"{report.reassigned_jobs} active job(s) reassigned")
        if report.unassigned_jobs:
            details.append(f"{report.unassigned_jobs} pending job(s) unassigned")
        suffix = f" ({', '.join(details)})" if details else ""
        return OperationResult.ok(report, f"Mechanic {report.name} deleted{suffix}.")

    def add_part(self, name: str, qty: str, price: str) -> OperationResult[int]:
        """Add a new inventory item."""
        return self._run(lambda: self._model.add_part(
            name, self._int(qty, "Opening stock"), self._money(price, "Unit price")), "Part added.")

    def restock_part(self, part_id: Optional[int], qty: str) -> OperationResult[SparePart]:
        """Increase stock of an existing part."""
        def _restock() -> SparePart:
            if part_id is None:
                raise ValidationError("Select a part to restock.")
            return self._model.restock_part(part_id, self._int(qty, "Quantity", 1))
        return self._run(_restock, "Stock updated.")

    def create_job(self, plate: str, mechanic_id: Optional[int], labor: str, description: str,
                   job_date: date, service_id: Optional[int] = None) -> OperationResult[int]:
        """Open a service job (optionally booked against a catalogue service)."""
        def _create() -> int:
            if not plate:
                raise ValidationError("Select a vehicle.")
            return self._model.create_job(plate, mechanic_id, self._money(labor, "Labour cost"),
                                          description, job_date, service_id)
        return self._run(_create, "Service job created.")

    def update_job_status(self, job_id: int, status: str) -> OperationResult[ServiceJob]:
        """Advance a job's status."""
        return self._run(lambda: self._model.update_job_status(job_id, status),
                         f"Job #{job_id} → {status}.")

    def allocate_parts(self, job_id: Optional[int],
                       lines: Sequence[Tuple[int, int]]) -> OperationResult[Invoice]:
        """Run the ACID allocation for ``[(part_id, qty), ...]``."""
        def _allocate() -> Invoice:
            if job_id is None:
                raise ValidationError("Select a job first.")
            return self._model.allocate_parts(job_id, [AllocationRequest(p, q) for p, q in lines])
        return self._run(_allocate, "Parts allocated and bill updated (transaction committed).")
