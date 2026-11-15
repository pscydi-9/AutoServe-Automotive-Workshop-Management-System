"""
views.dashboard_view
====================

Authenticated workspace: navigation shell plus seven responsive pages.

========== ===========================================================
Page       Purpose
========== ===========================================================
Overview   KPI tiles, low-stock alerts, mechanic workload
Schedule   Kanban grid (Pending / In Progress / Completed) for a day
Jobs       Book jobs (catalogue + date picker), ACID allocation, invoice
Inventory  Spare-part catalogue, stock intake
Vehicles   Customer vehicle registry (10-digit phone interceptor)
Mechanics  Full CRUD; transactional delete with reassignment
Settings   Admin credential updates behind a fresh OTP challenge
========== ===========================================================

Responsiveness: on desktop/tablet a sidebar is shown; in mobile layout the
sidebar collapses into a top-bar dropdown, all :class:`ResponsiveGrid`
containers re-flow to a single column and tables hide low-priority columns.

All data access goes through :class:`controllers.workshop_controller.WorkshopController`.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, timedelta
from functools import partial
from tkinter import messagebox
from typing import Any, Callable, Dict, List, Optional, Tuple, Type

import customtkinter as ctk

from controllers.auth_controller import AuthController, OTPDispatchInfo
from controllers.base_controller import OperationResult
from controllers.workshop_controller import WorkshopController
from core.config import AppConfig
from models.auth_model import Session
from models.workshop_model import Invoice, Mechanic, ServiceItem, ServiceJob
from views.components import (
    LAYOUT,
    PALETTE,
    TYPO,
    Card,
    ColumnSpec,
    DataTable,
    DatePickerField,
    KeyedOptionMenu,
    LabeledEntry,
    LayoutMode,
    NotificationBar,
    PhoneEntry,
    ResponsiveGrid,
    SectionHeader,
    StatCard,
    StatusBadge,
    font,
    make_button,
)

Notifier = Callable[[str, str], None]


# =========================================================================== #
# Base page
# =========================================================================== #
class BasePage(ctk.CTkScrollableFrame):
    """Scrollable page with a header, shared helpers and a refresh contract."""

    title: str = ""
    subtitle: str = ""

    def __init__(self, master: Any, controller: WorkshopController, notify: Notifier,
                 on_data_changed: Callable[[], None]) -> None:
        super().__init__(master, fg_color="transparent")
        self.controller = controller
        self._notify = notify
        self._on_data_changed = on_data_changed
        self.tables: List[DataTable] = []
        self.dirty = True
        self.header = SectionHeader(self, self.title, self.subtitle)
        self.header.pack(fill="x", padx=LAYOUT.padding_sm, pady=(LAYOUT.padding_sm, LAYOUT.padding_md))

    def money(self, amount: Any) -> str:
        """Currency formatter shortcut."""
        return self.controller.format_money(amount)

    def report(self, result: OperationResult[Any], changed: bool = True) -> bool:
        """Surface ``result`` in the notification bar; propagate data change on success."""
        if result.success:
            if result.message:
                self._notify(result.message, "success")
            if changed:
                self._on_data_changed()
        else:
            self._notify(result.message, "error")
        return result.success

    def unwrap(self, result: OperationResult[Any], default: Any) -> Any:
        """Return ``result.data`` or report the error and return ``default``."""
        if result.success:
            return result.data
        self._notify(result.message, "error")
        return default

    def refresh(self) -> None:
        """Reload data from the controller. Override in subclasses."""
        self.dirty = False

    def apply_layout(self, mode: LayoutMode) -> None:
        """Adapt to ``mode``; default collapses table columns on mobile."""
        for table in self.tables:
            table.set_compact(mode is LayoutMode.MOBILE)


# =========================================================================== #
# Overview
# =========================================================================== #
class OverviewPage(BasePage):
    """KPI dashboard."""

    title = "Workshop overview"
    subtitle = "Live operational metrics"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        stats = ResponsiveGrid(self, min_item_width=230, max_columns=3)
        stats.pack(fill="x")
        self._open = stats.add(StatCard(stats, "Open jobs", accent=PALETTE.primary))
        self._done = stats.add(StatCard(stats, "Completed today", accent=PALETTE.success))
        self._revenue = stats.add(StatCard(stats, "Revenue this month", accent=PALETTE.accent))
        self._mechanics = stats.add(StatCard(stats, "Mechanics available", accent=PALETTE.success))
        self._low = stats.add(StatCard(stats, "Low-stock items", accent=PALETTE.danger))
        self._inventory = stats.add(StatCard(stats, "Inventory value", accent=PALETTE.warning))

        panels = ResponsiveGrid(self, min_item_width=380, max_columns=2)
        panels.pack(fill="x", pady=(LAYOUT.padding_sm, 0))
        low_card = panels.add(Card(panels, "Low-stock alerts",
                                   f"Parts at or below {self.controller.rules.low_stock_threshold} units"))
        self._low_table = DataTable(low_card.body, [
            ColumnSpec("part_name", "Part", 3),
            ColumnSpec("stock_quantity", "Stock", 1, "center"),
            ColumnSpec("unit_price", "Price", 1, "e", 2, self.money),
        ], key="part_id", row_text_color=lambda _r: PALETTE.danger,
            empty_text="All parts are sufficiently stocked.")
        self._low_table.pack(fill="x")
        load_card = panels.add(Card(panels, "Mechanic workload", "Active jobs per technician"))
        self._load_table = DataTable(load_card.body, [
            ColumnSpec("name", "Mechanic", 2),
            ColumnSpec("specialization", "Skill", 2, priority=2),
            ColumnSpec("status", "Status", 1, "center"),
            ColumnSpec("active_jobs", "Jobs", 1, "center"),
        ], key="mechanic_id",
            row_text_color=lambda r: PALETTE.status_color(r["status"]))
        self._load_table.pack(fill="x")
        self.tables = [self._low_table, self._load_table]

    def refresh(self) -> None:
        super().refresh()
        metrics = self.unwrap(self.controller.dashboard_metrics(), None)
        if metrics:
            self._open.set_value(str(metrics.open_jobs), "Pending + in progress")
            self._done.set_value(str(metrics.completed_today), date.today().strftime("%d %b %Y"))
            self._revenue.set_value(self.money(metrics.revenue_month), "Completed jobs")
            self._mechanics.set_value(f"{metrics.available_mechanics}/{metrics.total_mechanics}",
                                      "Ready for assignment")
            self._low.set_value(str(metrics.low_stock_items), "Reorder recommended")
            self._inventory.set_value(self.money(metrics.inventory_value),
                                      f"{metrics.registered_vehicles} vehicles on file")
        self._low_table.set_rows([asdict(p) for p in self.unwrap(self.controller.low_stock(), [])])
        self._load_table.set_rows([asdict(m) for m in self.unwrap(self.controller.mechanics(), [])])


# =========================================================================== #
# Schedule board
# =========================================================================== #
class SchedulePage(BasePage):
    """Responsive Kanban grid of the day's jobs."""

    title = "Service schedule"
    subtitle = "Daily planner — open jobs from earlier days carry over"
    _NEXT_ACTION = {"Pending": ("Start", "In Progress", "primary"),
                    "In Progress": ("Complete", "Completed", "success")}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._day = date.today()
        actions = self.header.actions
        make_button(actions, "◀", lambda: self._shift(-1), "secondary", width=36).pack(side="left")
        self._day_picker = DatePickerField(actions, label=None, initial=self._day,
                                           command=self._pick_day, width=170)
        self._day_picker.pack(side="left", padx=4)
        make_button(actions, "▶", lambda: self._shift(1), "secondary", width=36).pack(side="left")
        make_button(actions, "Today", self._today, "ghost", width=60).pack(side="left", padx=(4, 0))
        self._board = ResponsiveGrid(self, min_item_width=280, max_columns=3)
        self._board.pack(fill="both", expand=True)

    def _shift(self, days: int) -> None:
        self._day += timedelta(days=days)
        self.refresh()

    def _today(self) -> None:
        self._day = date.today()
        self.refresh()

    def _pick_day(self, chosen: date) -> None:
        self._day = chosen
        self.refresh()

    def refresh(self) -> None:
        super().refresh()
        result = self.controller.schedule(self._day)
        if not result.success or result.data is None:
            self._notify(result.message, "error")
            return
        self._day, board = result.data
        self._day_picker.set_date(self._day)
        self._board.clear()
        for status in ("Pending", "In Progress", "Completed"):
            jobs = board.get(status, [])
            column = self._board.add(Card(self._board, f"{status}  ·  {len(jobs)}"))
            if not jobs:
                ctk.CTkLabel(column.body, text="No jobs", text_color=PALETTE.text_muted,
                             font=font(TYPO.size_body)).pack(pady=12)
            for job in jobs:
                self._job_tile(column.body, job).pack(fill="x", pady=4)

    def _job_tile(self, master: Any, job: ServiceJob) -> ctk.CTkFrame:
        tile = ctk.CTkFrame(master, fg_color=PALETTE.surface_alt, corner_radius=10)
        top = ctk.CTkFrame(tile, fg_color="transparent")
        top.pack(fill="x", padx=10, pady=(8, 0))
        ctk.CTkLabel(top, text=f"#{job.job_id} · {job.license_plate}",
                     font=font(TYPO.size_body, "bold"), text_color=PALETTE.text_primary).pack(side="left")
        StatusBadge(top, job.status).pack(side="right")
        details = f"{job.vehicle_model} — {job.owner_name}\nMechanic: {job.mechanic_name or 'Unassigned'}"
        if job.service_name:
            details += f"\nService: {job.service_name}"
        if job.job_date < self._day:
            details += f"\n⏳ Carried over from {job.job_date:%d %b}"
        if job.description and job.description != job.service_name:
            details += f"\nNote: {job.description}"
        ctk.CTkLabel(tile, text=details, justify="left", anchor="w", font=font(TYPO.size_caption),
                     text_color=PALETTE.text_muted).pack(fill="x", padx=10, pady=4)
        bottom = ctk.CTkFrame(tile, fg_color="transparent")
        bottom.pack(fill="x", padx=10, pady=(0, 8))
        ctk.CTkLabel(bottom, text=self.money(job.total_bill), font=font(TYPO.size_heading, "bold"),
                     text_color=PALETTE.text_primary).pack(side="left")
        if job.status in self._NEXT_ACTION:
            label, target, variant = self._NEXT_ACTION[job.status]
            make_button(bottom, label, partial(self._advance, job.job_id, target),
                        variant, width=90, height=30).pack(side="right")
        return tile

    def _advance(self, job_id: int, status: str) -> None:
        self.report(self.controller.update_job_status(job_id, status))


# =========================================================================== #
# Jobs, allocation & billing
# =========================================================================== #
class JobsPage(BasePage):
    """Job intake, ACID allocation and invoice preview."""

    title = "Service jobs"
    subtitle = "Create jobs, allocate parts transactionally and generate invoices"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._selected_job: Optional[ServiceJob] = None
        self._cart: Dict[int, int] = {}
        self._part_labels: Dict[int, str] = {}
        self._services: Dict[int, ServiceItem] = {}

        grid = ResponsiveGrid(self, min_item_width=360, max_columns=2)
        grid.pack(fill="x")
        self._build_create_form(grid.add(Card(grid, "New service job",
                                              "Assigning a mechanic marks them Busy")))
        self._build_detail_panel(grid.add(Card(grid, "Job details & parts allocation",
                                               "Allocation runs as a single ACID transaction")))
        table_card = grid.add(Card(grid, "All jobs"), span=2)
        self._build_table(table_card)
        catalog_card = grid.add(Card(grid, "Repairs catalogue",
                                     "Click a service to pre-fill the booking form"), span=2)
        self._catalog = DataTable(catalog_card.body, [
            ColumnSpec("service_name", "Service", 4),
            ColumnSpec("category", "Category", 2, priority=2),
            ColumnSpec("estimated_hours", "Est. time", 1, "center", 2, lambda h: f"{h} h"),
            ColumnSpec("base_labor_cost", "Labour", 2, "e", formatter=self.money),
        ], key="service_id", on_select=lambda r: self._select_service(r["service_id"]))
        self._catalog.pack(fill="x")
        self.tables.append(self._catalog)
        self._render_detail()

    # -- construction -------------------------------------------------- #
    def _build_create_form(self, card: Card) -> None:
        body = card.body
        self._service = KeyedOptionMenu(body, "Repair / service package", command=self._on_service_change)
        self._service.pack(fill="x")
        self._vehicle = KeyedOptionMenu(body, "Vehicle")
        self._vehicle.pack(fill="x")
        self._mechanic = KeyedOptionMenu(body, "Mechanic")
        self._mechanic.pack(fill="x")
        row = ctk.CTkFrame(body, fg_color="transparent")
        row.pack(fill="x")
        row.grid_columnconfigure((0, 1), weight=1, uniform="r")
        self._labor = LabeledEntry(row, f"Labour cost ({self.controller.rules.currency_symbol})",
                                   "1200.00", width=120)
        self._labor.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self._job_date = DatePickerField(row, "Booking date", min_date=date.today(), width=120)
        self._job_date.grid(row=0, column=1, sticky="new", padx=(6, 0))
        self._description = LabeledEntry(body, "Work description", "e.g. Periodic service")
        self._description.pack(fill="x")
        make_button(body, "Create job", self._create_job, height=40).pack(fill="x", pady=(4, 0))

    def _build_detail_panel(self, card: Card) -> None:
        body = card.body
        head = ctk.CTkFrame(body, fg_color="transparent")
        head.pack(fill="x")
        self._detail_title = ctk.CTkLabel(head, text="", font=font(TYPO.size_heading, "bold"),
                                          text_color=PALETTE.text_primary, anchor="w")
        self._detail_title.pack(side="left")
        self._detail_badge = StatusBadge(head, "")
        self._detail_info = ctk.CTkLabel(body, text="", justify="left", anchor="w",
                                         font=font(TYPO.size_caption), text_color=PALETTE.text_muted)
        self._detail_info.pack(fill="x", pady=(2, 6))

        actions = ctk.CTkFrame(body, fg_color="transparent")
        actions.pack(fill="x")
        self._btn_start = make_button(actions, "Start", lambda: self._set_status("In Progress"),
                                      "primary", width=80, height=30)
        self._btn_complete = make_button(actions, "Complete", lambda: self._set_status("Completed"),
                                         "success", width=90, height=30)
        self._btn_cancel = make_button(actions, "Cancel job", lambda: self._set_status("Cancelled"),
                                       "danger", width=90, height=30)
        for btn in (self._btn_start, self._btn_complete, self._btn_cancel):
            btn.pack(side="left", padx=(0, 6))

        ctk.CTkFrame(body, height=1, fg_color=PALETTE.border).pack(fill="x", pady=10)
        self._part = KeyedOptionMenu(body, "Spare part")
        self._part.pack(fill="x")
        qty_row = ctk.CTkFrame(body, fg_color="transparent")
        qty_row.pack(fill="x")
        qty_row.grid_columnconfigure(0, weight=1)
        self._qty = LabeledEntry(qty_row, "Quantity", "1", width=80)
        self._qty.grid(row=0, column=0, sticky="ew")
        self._btn_add_line = make_button(qty_row, "Add line", self._add_line, "secondary", width=90)
        self._btn_add_line.grid(row=0, column=1, padx=(8, 0), pady=(4, 0))
        self._cart_label = ctk.CTkLabel(body, text="", justify="left", anchor="w",
                                        font=font(TYPO.size_caption), text_color=PALETTE.text_primary)
        self._cart_label.pack(fill="x")
        cart_actions = ctk.CTkFrame(body, fg_color="transparent")
        cart_actions.pack(fill="x", pady=(4, 8))
        self._btn_commit = make_button(cart_actions, "Commit allocation", self._commit,
                                       "success", width=150)
        self._btn_commit.pack(side="left")
        make_button(cart_actions, "Clear", self._clear_cart, "ghost", width=60).pack(side="left", padx=6)

        self._invoice = ctk.CTkTextbox(body, height=190, corner_radius=10, wrap="none",
                                       font=ctk.CTkFont(family="Courier New", size=TYPO.size_caption),
                                       fg_color=PALETTE.surface_alt, text_color=PALETTE.text_primary)
        self._invoice.pack(fill="both", expand=True)

    def _build_table(self, card: Card) -> None:
        bar = ctk.CTkFrame(card.body, fg_color="transparent")
        bar.pack(fill="x", pady=(0, 8))
        self._filter = ctk.CTkSegmentedButton(
            bar, values=["All", *self.controller.rules.job_statuses], command=lambda _v: self.refresh(),
            font=font(TYPO.size_caption, "bold"), selected_color=PALETTE.primary)
        self._filter.set("All")
        self._filter.pack(side="left", fill="x", expand=True)
        self._search = ctk.CTkEntry(bar, placeholder_text="Search plate / owner", width=170,
                                    height=32, corner_radius=10)
        self._search.pack(side="right", padx=(8, 0))
        self._search.bind("<KeyRelease>", lambda _e: self.refresh())
        self._table = DataTable(card.body, [
            ColumnSpec("job_id", "#", 1, "center"),
            ColumnSpec("license_plate", "Plate", 2),
            ColumnSpec("vehicle_model", "Vehicle", 3, priority=2),
            ColumnSpec("service_name", "Service", 3, priority=2, formatter=lambda v: v or "Custom"),
            ColumnSpec("mechanic_name", "Mechanic", 2, priority=2,
                       formatter=lambda v: v or "Unassigned"),
            ColumnSpec("job_date", "Date", 2, priority=2, formatter=lambda d: f"{d:%d %b %Y}"),
            ColumnSpec("status", "Status", 2, "center"),
            ColumnSpec("total_bill", "Bill", 2, "e", formatter=self.money),
        ], key="job_id", on_select=self._on_select,
            row_text_color=lambda r: PALETTE.status_color(r["status"]) if r["status"] == "Cancelled" else None)
        self._table.pack(fill="x")
        self.tables = [self._table]

    # -- data ---------------------------------------------------------- #
    def refresh(self) -> None:
        super().refresh()
        services = self.unwrap(self.controller.services(), [])
        self._services = {s.service_id: s for s in services}
        self._service.set_items([(None, "Custom job — enter labour manually")] + [
            (s.service_id, f"{s.service_name} · {self.money(s.base_labor_cost)}") for s in services])
        self._catalog.set_rows([asdict(s) for s in services])
        self._job_date.set_min_date(date.today())
        if self._job_date.get_date() < date.today():
            self._job_date.set_date(date.today())
        vehicles = self.unwrap(self.controller.vehicles(), [])
        self._vehicle.set_items([(v.license_plate, f"{v.license_plate} — {v.model}") for v in vehicles])
        mechanics = self.unwrap(self.controller.mechanics(), [])
        self._mechanic.set_items([(None, "Unassigned")] + [
            (m.mechanic_id, f"{m.name} · {m.specialization} ({m.status})")
            for m in mechanics if m.status != "Off-Duty"])
        parts = self.unwrap(self.controller.parts(), [])
        self._part_labels = {p.part_id: p.part_name for p in parts}
        self._part.set_items([(p.part_id, f"{p.part_name} — {p.stock_quantity} in stock — "
                                          f"{self.money(p.unit_price)}") for p in parts])
        jobs = self.unwrap(self.controller.jobs(self._filter.get(), self._search.get()), [])
        self._table.set_rows([asdict(j) for j in jobs])
        if self._selected_job:
            self._selected_job = next((j for j in jobs if j.job_id == self._selected_job.job_id),
                                      self._selected_job)
        self._render_detail()

    def _on_select(self, row: Dict[str, Any]) -> None:
        self._selected_job = ServiceJob(**row)
        self._cart.clear()
        self._render_detail()

    def _render_detail(self) -> None:
        job = self._selected_job
        active = bool(job and job.status in self.controller.rules.active_job_statuses)
        if job is None:
            self._detail_title.configure(text="No job selected")
            self._detail_badge.pack_forget()
            self._detail_info.configure(text="Select a job from the table below to manage it.")
        else:
            self._detail_title.configure(text=f"Job #{job.job_id} · {job.license_plate}")
            self._detail_badge.set_status(job.status)
            self._detail_badge.pack(side="right")
            service = f"\nService: {job.service_name}" if job.service_name else ""
            self._detail_info.configure(
                text=f"{job.vehicle_model} — {job.owner_name}\n"
                     f"Mechanic: {job.mechanic_name or 'Unassigned'}   ·   {job.job_date:%d %b %Y}{service}")
        self._btn_start.configure(state="normal" if job and job.status == "Pending" else "disabled")
        self._btn_complete.configure(state="normal" if job and job.status == "In Progress" else "disabled")
        self._btn_cancel.configure(state="normal" if active else "disabled")
        self._btn_add_line.configure(state="normal" if active else "disabled")
        self._btn_commit.configure(state="normal" if active and self._cart else "disabled")
        self._cart_label.configure(text="\n".join(
            f"• {self._part_labels.get(pid, f'Part #{pid}')} × {qty}" for pid, qty in self._cart.items())
            or ("Allocation basket is empty." if active else ""))
        self._render_invoice()

    def _render_invoice(self) -> None:
        self._invoice.configure(state="normal")
        self._invoice.delete("1.0", "end")
        if self._selected_job:
            result = self.controller.invoice(self._selected_job.job_id)
            self._invoice.insert("1.0", self._format_invoice(result.data) if result.success
                                 else result.message)
        else:
            self._invoice.insert("1.0", "Invoice preview appears here.")
        self._invoice.configure(state="disabled")

    def _format_invoice(self, inv: Optional[Invoice]) -> str:
        if inv is None:
            return ""
        width = 46
        lines = [f"INVOICE  JOB #{inv.job.job_id}".center(width), "=" * width,
                 f"Vehicle : {inv.job.license_plate}  {inv.job.vehicle_model}"[:width],
                 f"Owner   : {inv.job.owner_name}  {inv.owner_phone}"[:width],
                 f"Status  : {inv.job.status}", "-" * width,
                 f"{'Item':<22}{'Qty':>4}{'Rate':>10}{'Amount':>10}", "-" * width]
        for line in inv.lines:
            lines.append(f"{line.part_name[:21]:<22}{line.quantity:>4}"
                         f"{line.unit_price:>10,.2f}{line.line_total:>10,.2f}")
        if not inv.lines:
            lines.append("(no parts allocated)")
        cs = self.controller.rules.currency_symbol
        lines += ["-" * width,
                  f"{'Parts subtotal':<30}{cs}{inv.parts_subtotal:>14,.2f}",
                  f"{'Labour':<30}{cs}{inv.job.labor_cost:>14,.2f}",
                  "=" * width,
                  f"{'TOTAL':<30}{cs}{inv.job.total_bill:>14,.2f}"]
        return "\n".join(lines)

    # -- commands ------------------------------------------------------ #
    def _select_service(self, service_id: Optional[int]) -> None:
        self._service.select_key(service_id)
        self._on_service_change(service_id)

    def _on_service_change(self, service_id: Optional[int]) -> None:
        service = self._services.get(service_id) if service_id is not None else None
        if service is None:
            return
        self._labor.set(f"{service.base_labor_cost:.2f}")
        self._description.set(service.service_name)

    def _create_job(self) -> None:
        result = self.controller.create_job(self._vehicle.selected_key, self._mechanic.selected_key,
                                            self._labor.get(), self._description.get(),
                                            self._job_date.get_date(), self._service.selected_key)
        if self.report(result):
            self._labor.clear()
            self._description.clear()
            self._service.select_key(None)
            self._job_date.set_date(date.today())

    def _set_status(self, status: str) -> None:
        if self._selected_job:
            result = self.controller.update_job_status(self._selected_job.job_id, status)
            if result.success and result.data:
                self._selected_job = result.data
            self.report(result)

    def _add_line(self) -> None:
        part_id = self._part.selected_key
        try:
            qty = int(self._qty.get() or "1")
            if qty <= 0:
                raise ValueError
        except ValueError:
            self._qty.set_error("Positive whole number")
            return
        self._qty.set_error("")
        if part_id is None:
            self._notify("Select a spare part.", "warning")
            return
        self._cart[part_id] = self._cart.get(part_id, 0) + qty
        self._render_detail()

    def _clear_cart(self) -> None:
        self._cart.clear()
        self._render_detail()

    def _commit(self) -> None:
        if not self._selected_job:
            return
        result = self.controller.allocate_parts(self._selected_job.job_id, list(self._cart.items()))
        if result.success and result.data:
            self._cart.clear()
            self._selected_job = result.data.job
        self.report(result)
        if not result.success:
            self._render_detail()


# =========================================================================== #
# Inventory
# =========================================================================== #
class InventoryPage(BasePage):
    """Spare-part catalogue and stock intake."""

    title = "Inventory"
    subtitle = "Stock levels are protected by CHECK (stock_quantity >= 0)"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        grid = ResponsiveGrid(self, min_item_width=320, max_columns=2)
        grid.pack(fill="x")
        add = grid.add(Card(grid, "Add spare part")).body
        self._name = LabeledEntry(add, "Part name", "e.g. Cabin Air Filter")
        self._name.pack(fill="x")
        row = ctk.CTkFrame(add, fg_color="transparent")
        row.pack(fill="x")
        row.grid_columnconfigure((0, 1), weight=1, uniform="r")
        self._stock = LabeledEntry(row, "Opening stock", "10", width=100)
        self._stock.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self._price = LabeledEntry(row, f"Unit price ({self.controller.rules.currency_symbol})",
                                   "499.00", width=100)
        self._price.grid(row=0, column=1, sticky="ew", padx=(6, 0))
        make_button(add, "Add part", self._add, height=40).pack(fill="x")

        restock = grid.add(Card(grid, "Stock intake", "Receive goods against an existing part")).body
        self._restock_part = KeyedOptionMenu(restock, "Part")
        self._restock_part.pack(fill="x")
        self._restock_qty = LabeledEntry(restock, "Quantity received", "5")
        self._restock_qty.pack(fill="x")
        make_button(restock, "Receive stock", self._restock, "success", height=40).pack(fill="x")

        table_card = grid.add(Card(grid, "Catalogue"), span=2)
        self._search = ctk.CTkEntry(table_card.body, placeholder_text="Search parts", height=32,
                                    corner_radius=10)
        self._search.pack(fill="x", pady=(0, 8))
        self._search.bind("<KeyRelease>", lambda _e: self.refresh())
        threshold = self.controller.rules.low_stock_threshold
        self._table = DataTable(table_card.body, [
            ColumnSpec("part_id", "ID", 1, "center", priority=2),
            ColumnSpec("part_name", "Part", 4),
            ColumnSpec("stock_quantity", "Stock", 1, "center"),
            ColumnSpec("unit_price", "Unit price", 2, "e", formatter=self.money),
            ColumnSpec("stock_value", "Stock value", 2, "e", priority=2, formatter=self.money),
        ], key="part_id", on_select=lambda r: self._restock_part.select_key(r["part_id"]),
            row_text_color=lambda r: PALETTE.danger if r["stock_quantity"] <= threshold else None)
        self._table.pack(fill="x")
        self.tables = [self._table]

    def refresh(self) -> None:
        super().refresh()
        parts = self.unwrap(self.controller.parts(self._search.get()), [])
        self._table.set_rows([{**asdict(p), "stock_value": p.stock_value} for p in parts])
        all_parts = parts if not self._search.get() else self.unwrap(self.controller.parts(), [])
        self._restock_part.set_items([(p.part_id, f"{p.part_name} ({p.stock_quantity})")
                                      for p in sorted(all_parts, key=lambda p: p.part_name)])

    def _add(self) -> None:
        if self.report(self.controller.add_part(self._name.get(), self._stock.get(), self._price.get())):
            for field in (self._name, self._stock, self._price):
                field.clear()

    def _restock(self) -> None:
        if self.report(self.controller.restock_part(self._restock_part.selected_key,
                                                    self._restock_qty.get())):
            self._restock_qty.clear()


# =========================================================================== #
# Vehicles
# =========================================================================== #
class VehiclesPage(BasePage):
    """Customer vehicle registry."""

    title = "Vehicles"
    subtitle = "Customer fleet and owner contacts"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        grid = ResponsiveGrid(self, min_item_width=340, max_columns=2)
        grid.pack(fill="x")
        form = grid.add(Card(grid, "Register vehicle")).body
        self._plate = LabeledEntry(form, "Licence plate", "MH12AB1234")
        self._model = LabeledEntry(form, "Make & model", "Hyundai i20")
        self._owner = LabeledEntry(form, "Owner name", "Full name")
        self._phone = PhoneEntry(form, "Owner phone (10 digits)", "10-digit mobile number")
        self._email = LabeledEntry(form, "Owner e-mail (optional)", "owner@mail.com")
        for field in (self._plate, self._model, self._owner, self._phone, self._email):
            field.pack(fill="x")
        make_button(form, "Register", self._on_register_vehicle, height=40).pack(fill="x")

        table_card = grid.add(Card(grid, "Registry"), span=2)
        self._search = ctk.CTkEntry(table_card.body, placeholder_text="Search plate / model / owner",
                                    height=32, corner_radius=10)
        self._search.pack(fill="x", pady=(0, 8))
        self._search.bind("<KeyRelease>", lambda _e: self.refresh())
        self._table = DataTable(table_card.body, [
            ColumnSpec("license_plate", "Plate", 2),
            ColumnSpec("model", "Model", 3),
            ColumnSpec("owner_name", "Owner", 3, priority=2),
            ColumnSpec("owner_phone", "Phone", 3, priority=2),
            ColumnSpec("owner_email", "E-mail", 3, priority=2, formatter=lambda v: v or "—"),
        ], key="license_plate")
        self._table.pack(fill="x")
        self.tables = [self._table]

    def refresh(self) -> None:
        super().refresh()
        self._table.set_rows([asdict(v) for v in
                              self.unwrap(self.controller.vehicles(self._search.get()), [])])

    def _on_register_vehicle(self) -> None:
        if not self._phone.is_valid():
            self._notify("Owner phone must be exactly 10 digits.", "error")
            return
        fields = (self._plate, self._model, self._owner, self._phone, self._email)
        if self.report(self.controller.register_vehicle(*(f.get() for f in fields))):
            for field in fields:
                field.clear()


# =========================================================================== #
# Mechanics
# =========================================================================== #
class MechanicsPage(BasePage):
    """Mechanic CRUD: create, read (roster), update and transactional delete."""

    title = "Mechanics"
    subtitle = "Create, edit and safely remove technicians — active job cards are never orphaned"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._mechanics: Dict[int, Mechanic] = {}
        grid = ResponsiveGrid(self, min_item_width=320, max_columns=2)
        grid.pack(fill="x")

        add = grid.add(Card(grid, "Add mechanic", "New technicians start as Available")).body
        self._name = LabeledEntry(add, "Name", "Full name")
        self._spec = LabeledEntry(add, "Specialization", "e.g. Diesel engines")
        self._name.pack(fill="x")
        self._spec.pack(fill="x")
        make_button(add, "Add mechanic", self._add, height=40).pack(fill="x")

        edit = grid.add(Card(grid, "Edit / remove mechanic",
                             "Select from the dropdown or click a roster row")).body
        self._who = KeyedOptionMenu(edit, "Mechanic", command=self._load_selected)
        self._who.pack(fill="x")
        self._edit_name = LabeledEntry(edit, "Name", "Full name")
        self._edit_name.pack(fill="x")
        self._edit_spec = LabeledEntry(edit, "Specialization", "Specialization")
        self._edit_spec.pack(fill="x")
        self._status = KeyedOptionMenu(edit, "Status")
        self._status.set_items([(s, s) for s in self.controller.rules.mechanic_statuses])
        self._status.pack(fill="x")
        make_button(edit, "Save changes", self._update, height=38).pack(fill="x")

        ctk.CTkFrame(edit, height=1, fg_color=PALETTE.border).pack(fill="x", pady=12)
        self._workload = ctk.CTkLabel(edit, text="", font=font(TYPO.size_caption), anchor="w",
                                      justify="left", text_color=PALETTE.text_muted, wraplength=300)
        self._workload.pack(fill="x")
        self._reassign = KeyedOptionMenu(edit, "Reassign active job cards to")
        self._reassign.pack(fill="x")
        make_button(edit, "Delete mechanic", self._delete, "danger", height=38).pack(fill="x")

        table_card = grid.add(Card(grid, "Roster"), span=2)
        self._table = DataTable(table_card.body, [
            ColumnSpec("mechanic_id", "ID", 1, "center", priority=2),
            ColumnSpec("name", "Name", 3),
            ColumnSpec("specialization", "Specialization", 3, priority=2),
            ColumnSpec("status", "Status", 2, "center"),
            ColumnSpec("active_jobs", "Active jobs", 1, "center"),
        ], key="mechanic_id", on_select=self._on_select,
            row_text_color=lambda r: PALETTE.status_color(r["status"]))
        self._table.pack(fill="x")
        self.tables = [self._table]

    def refresh(self) -> None:
        super().refresh()
        mechanics = self.unwrap(self.controller.mechanics(), [])
        self._mechanics = {m.mechanic_id: m for m in mechanics}
        self._table.set_rows([asdict(m) for m in mechanics])
        self._who.set_items([(m.mechanic_id, f"{m.name} ({m.status})") for m in mechanics])
        self._load_selected(self._who.selected_key)

    def _load_selected(self, mechanic_id: Optional[int]) -> None:
        mechanic = self._mechanics.get(mechanic_id) if mechanic_id is not None else None
        if mechanic is None:
            for field in (self._edit_name, self._edit_spec):
                field.clear()
            self._workload.configure(text="No mechanic selected.")
            self._reassign.set_items([(None, "—")])
            return
        self._edit_name.set(mechanic.name)
        self._edit_spec.set(mechanic.specialization)
        self._status.select_key(mechanic.status)
        self._workload.configure(
            text=f"{mechanic.name} has {mechanic.active_jobs} active job card(s). Deleting is blocked "
                 "while any are In Progress unless you pick a mechanic to take them over.")
        self._reassign.set_items(
            [(None, "Don't reassign (pending jobs become unassigned)")] +
            [(m.mechanic_id, f"{m.name} · {m.specialization}") for m in self._mechanics.values()
             if m.mechanic_id != mechanic.mechanic_id and m.status != "Off-Duty"],
            keep_selection=False)

    def _on_select(self, row: Dict[str, Any]) -> None:
        self._who.select_key(row["mechanic_id"])
        self._load_selected(row["mechanic_id"])

    def _add(self) -> None:
        if self.report(self.controller.add_mechanic(self._name.get(), self._spec.get())):
            self._name.clear()
            self._spec.clear()

    def _update(self) -> None:
        self.report(self.controller.update_mechanic(self._who.selected_key, self._edit_name.get(),
                                                    self._edit_spec.get(), self._status.selected_key))

    def _delete(self) -> None:
        mechanic = self._mechanics.get(self._who.selected_key)
        if mechanic is None:
            self._notify("Select a mechanic first.", "warning")
            return
        target = self._reassign.selected_key
        detail = (f"Active job cards will move to {self._mechanics[target].name}."
                  if target in self._mechanics else
                  "Pending jobs will become unassigned; the deletion is refused if any job is In Progress.")
        if not messagebox.askyesno("Delete mechanic",
                                   f"Delete {mechanic.name} permanently?\n\n{detail}",
                                   parent=self.winfo_toplevel()):
            return
        self.report(self.controller.delete_mechanic(mechanic.mechanic_id, target))


# =========================================================================== #
# Admin settings
# =========================================================================== #
class AdminSettingsPage(BasePage):
    """Credential management protected by a fresh e-mail OTP challenge."""

    title = "Admin settings"
    subtitle = "Changes to your username, e-mail, phone or password apply only after OTP verification"

    def __init__(self, master: Any, controller: WorkshopController, notify: Notifier,
                 on_data_changed: Callable[[], None], auth_controller: AuthController,
                 on_session_changed: Callable[[Session], None]) -> None:
        """
        :param auth_controller: Issues and verifies the credential-change OTP.
        :param on_session_changed: Receives the rotated session after a successful update.
        """
        super().__init__(master, controller, notify, on_data_changed)
        self._auth = auth_controller
        self._on_session_changed = on_session_changed
        self._tick_job: Optional[str] = None
        self._pending = False

        grid = ResponsiveGrid(self, min_item_width=360, max_columns=2)
        grid.pack(fill="x")
        profile = grid.add(Card(grid, "Account profile", "Sign-in identity and OTP destinations")).body
        self._username = LabeledEntry(profile, "Username", "3–40 letters, digits, _ or .")
        self._email = LabeledEntry(profile, "E-mail (receives sign-in codes)", "name@gmail.com")
        self._phone = PhoneEntry(profile, "Mobile (10 digits, optional — SMS mock)", required=False)
        for field in (self._username, self._email, self._phone):
            field.pack(fill="x")

        password = grid.add(Card(grid, "Password", "Leave the new password blank to keep it")).body
        self._current = LabeledEntry(password, "Current password (required)", "••••••••", show="•")
        self._new = LabeledEntry(password, "New password", "Min. 8 chars, letters + digits", show="•")
        self._confirm = LabeledEntry(password, "Confirm new password", "Repeat new password", show="•")
        for field in (self._current, self._new, self._confirm):
            field.pack(fill="x")
        self._save_btn = make_button(password, "Save changes — send verification code", self._on_save,
                                     height=40)
        self._save_btn.pack(fill="x")

        verify_card = grid.add(Card(grid, "Two-factor confirmation",
                                    "A new code is e-mailed to your CURRENT address"), span=2)
        body = verify_card.body
        self._verify_info = ctk.CTkLabel(body, text="No pending change.", font=font(TYPO.size_body),
                                         anchor="w", justify="left", text_color=PALETTE.text_muted)
        self._verify_info.pack(fill="x")
        row = ctk.CTkFrame(body, fg_color="transparent")
        row.pack(fill="x", pady=(6, 0))
        row.grid_columnconfigure(0, weight=1)
        self._otp = LabeledEntry(row, f"{self._auth.otp_length}-digit code", "• • • • • •",
                                 justify="center")
        self._otp.grid(row=0, column=0, sticky="ew")
        self._otp.entry.bind("<Return>", lambda _e: self._on_confirm())
        self._confirm_btn = make_button(row, "Confirm & apply", self._on_confirm, "success", width=150)
        self._confirm_btn.grid(row=0, column=1, padx=(8, 0), pady=(4, 0))
        self._cancel_btn = make_button(row, "Cancel", self._on_cancel, "ghost", width=80)
        self._cancel_btn.grid(row=0, column=2, padx=(4, 0), pady=(4, 0))
        self._countdown = ctk.CTkLabel(body, text="", font=font(TYPO.size_caption), anchor="w",
                                       text_color=PALETTE.text_muted)
        self._countdown.pack(fill="x")
        self._set_pending(False)

    # -- data ---------------------------------------------------------- #
    def refresh(self) -> None:
        super().refresh()
        user = self._auth.current_user
        if user is None or self._pending:
            return
        self._username.set(user.username)
        self._email.set(user.email)
        self._phone.set(user.phone or "")
        self._phone.set_hint("")

    def _set_pending(self, pending: bool) -> None:
        self._pending = pending
        if not pending:
            self._otp.set_enabled(True)
            self._otp.clear()
        self._otp.set_enabled(pending)
        self._confirm_btn.configure(state="normal" if pending else "disabled")
        self._cancel_btn.configure(state="normal" if pending else "disabled")
        if not pending:
            self._otp.clear()
            self._stop_ticker()
            self._countdown.configure(text="")
            self._verify_info.configure(text="No pending change.", text_color=PALETTE.text_muted)

    # -- commands ------------------------------------------------------ #
    def _on_save(self) -> None:
        if not self._phone.is_valid():
            return
        if not self._current.entry.get():
            self._current.set_error("Required to authorise changes")
            return
        self._current.set_error("")
        self._save_btn.configure(state="disabled", text="Verifying & sending code…")
        self._auth.request_credential_change(
            self._current.entry.get(), self._username.get(), self._email.get(), self._phone.get(),
            self._new.entry.get(), self._confirm.entry.get(), self._on_code_sent)

    def _on_code_sent(self, result: OperationResult[OTPDispatchInfo]) -> None:
        if not self.winfo_exists():
            return
        self._save_btn.configure(state="normal", text="Save changes — send verification code")
        if not result.success or result.data is None:
            self._notify(result.message, "error")
            return
        info = result.data
        self._set_pending(True)
        mock = "  (mock mode: see the terminal)" if info.is_mock else ""
        self._verify_info.configure(
            text=f"Code sent to {info.destination_masked}. Enter it to apply your changes.{mock}",
            text_color=PALETTE.text_primary)
        self._otp.entry.focus_set()
        self._notify("Verification code sent to your current e-mail.", "info")
        self._tick()

    def _on_confirm(self) -> None:
        code = self._otp.get()
        if len(code) != self._auth.otp_length or not code.isdigit():
            self._otp.set_error(f"Enter the {self._auth.otp_length}-digit code.")
            return
        self._otp.set_error("")
        result = self._auth.confirm_credential_change(code)
        if result.success and result.data is not None:
            for field in (self._current, self._new, self._confirm):
                field.clear()
            self._set_pending(False)
            self._on_session_changed(result.data)
            self._notify(result.message, "success")
            self.refresh()
            return
        self._notify(result.message, "error")
        if self._auth.change_seconds_remaining() == 0:
            self._set_pending(False)

    def _on_cancel(self) -> None:
        self._auth.cancel_credential_change()
        self._set_pending(False)
        self.refresh()
        self._notify("Pending credential change discarded.", "info")

    # -- countdown ----------------------------------------------------- #
    def _tick(self) -> None:
        remaining = self._auth.change_seconds_remaining()
        if remaining <= 0:
            self._countdown.configure(text="Code expired — save again to get a new one.",
                                      text_color=PALETTE.danger)
            self._confirm_btn.configure(state="disabled")
            self._tick_job = None
            return
        self._countdown.configure(text=f"Code expires in {remaining // 60}:{remaining % 60:02d}",
                                  text_color=PALETTE.warning if remaining < 60 else PALETTE.text_muted)
        self._tick_job = self.after(1000, self._tick)

    def _stop_ticker(self) -> None:
        if self._tick_job:
            self.after_cancel(self._tick_job)
            self._tick_job = None

    def destroy(self) -> None:
        """Cancel timers before teardown."""
        self._stop_ticker()
        super().destroy()


# =========================================================================== #
# Shell
# =========================================================================== #
class DashboardView(ctk.CTkFrame):
    """Navigation shell hosting the pages; adapts sidebar ↔ top-bar by layout mode."""

    PAGES: Tuple[Tuple[str, str, Type[BasePage]], ...] = (
        ("Overview", "◧", OverviewPage),
        ("Schedule", "▦", SchedulePage),
        ("Jobs", "⚙", JobsPage),
        ("Inventory", "▣", InventoryPage),
        ("Vehicles", "◈", VehiclesPage),
        ("Mechanics", "●", MechanicsPage),
        ("Admin Settings", "⚑", AdminSettingsPage),
    )

    def __init__(self, master: Any, controller: WorkshopController, auth_controller: AuthController,
                 session: Session, config: AppConfig, on_logout: Callable[[], None]) -> None:
        """
        :param master: Application root.
        :param controller: Workshop controller.
        :param auth_controller: Authentication controller (Admin Settings page).
        :param session: Authenticated session (for the user chip).
        :param config: Application configuration.
        :param on_logout: Invoked when the user signs out.
        """
        super().__init__(master, fg_color=PALETTE.background, corner_radius=0)
        self._controller = controller
        self._auth_controller = auth_controller
        self._session = session
        self._page_specs = [spec for spec in self.PAGES
                            if spec[2] is not AdminSettingsPage or session.user.role == "Admin"]
        self._config = config
        self._on_logout = on_logout
        self._mode: Optional[LayoutMode] = None
        self._current: Optional[str] = None
        self._nav_buttons: Dict[str, ctk.CTkButton] = {}

        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=1)
        self._sidebar = self._build_sidebar()
        main = ctk.CTkFrame(self, fg_color="transparent")
        main.grid(row=0, column=1, sticky="nsew")
        main.grid_rowconfigure(2, weight=1)
        main.grid_columnconfigure(0, weight=1)
        self._topbar = self._build_topbar(main)
        self._topbar.grid(row=0, column=0, sticky="ew", padx=LAYOUT.padding_md, pady=(LAYOUT.padding_md, 0))
        self._notice = NotificationBar(main)
        self._notice.grid(row=1, column=0, sticky="ew", padx=LAYOUT.padding_md, pady=6)
        self._content = ctk.CTkFrame(main, fg_color="transparent")
        self._content.grid(row=2, column=0, sticky="nsew", padx=LAYOUT.padding_sm, pady=(0, LAYOUT.padding_sm))
        self._content.grid_rowconfigure(0, weight=1)
        self._content.grid_columnconfigure(0, weight=1)

        self._pages: Dict[str, BasePage] = {}
        for name, _icon, cls in self._page_specs:
            if cls is AdminSettingsPage:
                self._pages[name] = AdminSettingsPage(self._content, controller, self.notify,
                                                      self._mark_all_dirty, auth_controller,
                                                      self._on_session_changed)
            else:
                self._pages[name] = cls(self._content, controller, self.notify, self._mark_all_dirty)
        self.apply_layout(LayoutMode.DESKTOP)
        self.show_page("Overview")
        self.notify(f"Welcome back, {session.user.full_name}.", "success")

    # -- construction -------------------------------------------------- #
    def _build_sidebar(self) -> ctk.CTkFrame:
        bar = ctk.CTkFrame(self, width=LAYOUT.sidebar_width, corner_radius=0, fg_color=PALETTE.surface,
                           border_width=0)
        bar.grid_propagate(False)
        ctk.CTkLabel(bar, text="AutoServe Pro", font=font(TYPO.size_title, "bold"),
                     text_color=PALETTE.primary).pack(padx=20, pady=(24, 2), anchor="w")
        ctk.CTkLabel(bar, text="Workshop CRM", font=font(TYPO.size_caption),
                     text_color=PALETTE.text_muted).pack(padx=20, anchor="w", pady=(0, 20))
        for name, icon, _cls in self._page_specs:
            btn = ctk.CTkButton(bar, text=f"  {icon}   {name}", anchor="w", height=40, corner_radius=10,
                                font=font(TYPO.size_body, "bold"), fg_color="transparent",
                                text_color=PALETTE.text_primary, hover_color=PALETTE.surface_alt,
                                command=lambda n=name: self.show_page(n))
            btn.pack(fill="x", padx=12, pady=2)
            self._nav_buttons[name] = btn
        footer = ctk.CTkFrame(bar, fg_color="transparent")
        footer.pack(side="bottom", fill="x", padx=12, pady=16)
        self._theme_switch = ctk.CTkSwitch(footer, text="Dark mode", command=self._toggle_theme,
                                           font=font(TYPO.size_caption))
        if ctk.get_appearance_mode().lower() == "dark":
            self._theme_switch.select()
        self._theme_switch.pack(anchor="w", pady=(0, 10))
        make_button(footer, "Sign out", self._logout, "secondary", height=34).pack(fill="x")
        return bar

    def _build_topbar(self, master: Any) -> ctk.CTkFrame:
        top = ctk.CTkFrame(master, fg_color=PALETTE.surface, corner_radius=LAYOUT.corner_radius,
                           border_width=1, border_color=PALETTE.border, height=56)
        top.grid_columnconfigure(1, weight=1)
        self._mobile_nav = ctk.CTkOptionMenu(top, values=[n for n, _i, _c in self._page_specs], width=150,
                                             command=self.show_page, font=font(TYPO.size_body, "bold"),
                                             fg_color=PALETTE.primary, button_color=PALETTE.primary_hover)
        self._page_title = ctk.CTkLabel(top, text="", font=font(TYPO.size_heading, "bold"),
                                        text_color=PALETTE.text_primary, anchor="w")
        self._page_title.grid(row=0, column=1, sticky="w", padx=14, pady=12)
        user = self._session.user
        self._user_chip = ctk.CTkLabel(top, text=f"● {user.full_name} ({user.username}) · {user.role}",
                                       font=font(TYPO.size_caption, "bold"), text_color=PALETTE.text_muted)
        self._user_chip.grid(row=0, column=2, padx=14)
        self._mobile_logout = make_button(top, "⎋", self._logout, "secondary", width=36, height=32)
        return top

    # -- navigation ---------------------------------------------------- #
    def show_page(self, name: str) -> None:
        """Bring page ``name`` to front and refresh it if stale."""
        if self._current and self._current in self._pages:
            self._pages[self._current].grid_forget()
        self._current = name
        page = self._pages[name]
        page.grid(row=0, column=0, sticky="nsew")
        if page.dirty:
            page.refresh()
        page.apply_layout(self._mode or LayoutMode.DESKTOP)
        self._page_title.configure(text=page.title)
        self._mobile_nav.set(name)
        for key, btn in self._nav_buttons.items():
            btn.configure(fg_color=PALETTE.primary if key == name else "transparent",
                          text_color=PALETTE.text_on_primary if key == name else PALETTE.text_primary)

    def _on_session_changed(self, session: Session) -> None:
        """Adopt the rotated session after a credential change."""
        self._session = session
        user = session.user
        self._user_chip.configure(text=f"● {user.full_name} ({user.username}) · {user.role}")

    def _mark_all_dirty(self) -> None:
        for page in self._pages.values():
            page.dirty = True
        if self._current:
            self._pages[self._current].refresh()

    def notify(self, message: str, level: str = "info") -> None:
        """Show a transient banner message."""
        self._notice.show(message, level)

    # -- responsiveness ------------------------------------------------ #
    def apply_layout(self, mode: LayoutMode) -> None:
        """Switch between sidebar (desktop/tablet) and top-bar navigation (mobile)."""
        if mode == self._mode:
            return
        self._mode = mode
        if mode is LayoutMode.MOBILE:
            self._sidebar.grid_forget()
            self._mobile_nav.grid(row=0, column=0, padx=(10, 0), pady=10)
            self._page_title.grid_remove()
            self._user_chip.grid_remove()
            self._mobile_logout.grid(row=0, column=3, padx=10)
        else:
            self._sidebar.configure(width=LAYOUT.sidebar_width if mode is LayoutMode.DESKTOP else 180)
            self._sidebar.grid(row=0, column=0, sticky="nsw")
            self._mobile_nav.grid_remove()
            self._mobile_logout.grid_remove()
            self._page_title.grid()
            self._user_chip.grid()
        if self._current:
            self._pages[self._current].apply_layout(mode)
        for page in self._pages.values():
            page.apply_layout(mode)

    # -- actions ------------------------------------------------------- #
    def _toggle_theme(self) -> None:
        ctk.set_appearance_mode("dark" if self._theme_switch.get() else "light")

    def _logout(self) -> None:
        self._on_logout()
