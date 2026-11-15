"""
views.components
================

Reusable, theme-aware CustomTkinter building blocks.

Every visual token (colour, font size, radius, spacing) comes from
:mod:`core.config`, so the look of the whole application can be changed in one
place. Components are deliberately free of business logic: they render data
and raise callbacks.

Highlights
----------
* :class:`MainThreadDispatcher` — thread-safe bridge that lets worker threads
  schedule UI updates on the Tk event loop.
* :class:`ResponsiveGrid` — re-flows children into 1…N columns as the window
  (or phone screen under Pydroid 3) is resized.
* :class:`DataTable` — lightweight, selectable, striped table whose low-priority
  columns collapse automatically in mobile layout.
"""

from __future__ import annotations

import queue
import re
import tkinter as tk
from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import customtkinter as ctk

from core.config import LayoutConfig, ThemeColor, get_config

_CFG = get_config()
PALETTE = _CFG.theme
TYPO = _CFG.typography
LAYOUT = _CFG.layout

_FONT_CACHE: Dict[Tuple[int, str], ctk.CTkFont] = {}


def font(size: int = TYPO.size_body, weight: str = "normal") -> ctk.CTkFont:
    """Return a cached :class:`ctk.CTkFont` for ``size``/``weight`` (requires a Tk root)."""
    key = (size, weight)
    if key not in _FONT_CACHE:
        _FONT_CACHE[key] = ctk.CTkFont(family=TYPO.family, size=size, weight=weight)
    return _FONT_CACHE[key]


# --------------------------------------------------------------------------- #
# Infrastructure
# --------------------------------------------------------------------------- #
class LayoutMode(str, Enum):
    """Responsive layout tiers driven by window width."""

    MOBILE = "mobile"
    TABLET = "tablet"
    DESKTOP = "desktop"

    @classmethod
    def from_width(cls, width: int, layout: LayoutConfig = LAYOUT) -> "LayoutMode":
        """Classify a pixel width against the configured breakpoints."""
        if width < layout.mobile_breakpoint:
            return cls.MOBILE
        if width < layout.tablet_breakpoint:
            return cls.TABLET
        return cls.DESKTOP


class MainThreadDispatcher:
    """
    Queue-based marshaller from worker threads to the Tk main loop.

    Tkinter is not thread-safe; background threads call :meth:`dispatch`, and a
    periodic ``after`` pump executes the queued callables on the UI thread.
    """

    def __init__(self, root: tk.Misc, interval_ms: int = 40) -> None:
        self._root = root
        self._interval = interval_ms
        self._queue: "queue.SimpleQueue[Callable[[], None]]" = queue.SimpleQueue()
        self._running = True
        self._root.after(self._interval, self._pump)

    def dispatch(self, callback: Callable[[], None]) -> None:
        """Schedule ``callback`` on the UI thread (safe from any thread)."""
        if self._running:
            self._queue.put(callback)

    def _pump(self) -> None:
        while True:
            try:
                callback = self._queue.get_nowait()
            except queue.Empty:
                break
            try:
                callback()
            except tk.TclError:
                pass  # target widget destroyed meanwhile
        if self._running:
            self._root.after(self._interval, self._pump)

    def stop(self) -> None:
        """Stop pumping (call before destroying the root)."""
        self._running = False


# --------------------------------------------------------------------------- #
# Primitive widgets
# --------------------------------------------------------------------------- #
_BUTTON_VARIANTS: Dict[str, Dict[str, Any]] = {
    "primary": {"fg_color": PALETTE.primary, "hover_color": PALETTE.primary_hover,
                "text_color": PALETTE.text_on_primary},
    "success": {"fg_color": PALETTE.success, "hover_color": ("#15803D", "#16A34A"),
                "text_color": PALETTE.text_on_primary},
    "danger": {"fg_color": PALETTE.danger, "hover_color": ("#B91C1C", "#DC2626"),
               "text_color": PALETTE.text_on_primary},
    "secondary": {"fg_color": PALETTE.surface_alt, "hover_color": PALETTE.border,
                  "text_color": PALETTE.text_primary},
    "ghost": {"fg_color": "transparent", "hover_color": PALETTE.surface_alt,
              "text_color": PALETTE.primary},
}


def make_button(master: Any, text: str, command: Optional[Callable[[], None]] = None,
                variant: str = "primary", width: int = 120, height: int = 36,
                **kwargs: Any) -> ctk.CTkButton:
    """Factory for consistently styled buttons (``primary``/``success``/``danger``/``secondary``/``ghost``)."""
    style = dict(_BUTTON_VARIANTS.get(variant, _BUTTON_VARIANTS["primary"]))
    style.update(kwargs)
    return ctk.CTkButton(master, text=text, command=command, width=width, height=height,
                         corner_radius=10, font=font(TYPO.size_body, "bold"), **style)


class Card(ctk.CTkFrame):
    """Rounded surface with an optional title/subtitle header and a ``body`` frame."""

    def __init__(self, master: Any, title: Optional[str] = None, subtitle: Optional[str] = None,
                 **kwargs: Any) -> None:
        kwargs.setdefault("fg_color", PALETTE.surface)
        kwargs.setdefault("corner_radius", LAYOUT.corner_radius)
        kwargs.setdefault("border_width", 1)
        kwargs.setdefault("border_color", PALETTE.border)
        super().__init__(master, **kwargs)
        pad = LAYOUT.padding_md
        if title:
            ctk.CTkLabel(self, text=title, font=font(TYPO.size_heading, "bold"),
                         text_color=PALETTE.text_primary, anchor="w").pack(
                fill="x", padx=pad, pady=(pad, 0))
        if subtitle:
            ctk.CTkLabel(self, text=subtitle, font=font(TYPO.size_caption),
                         text_color=PALETTE.text_muted, anchor="w", justify="left").pack(
                fill="x", padx=pad, pady=(2, 0))
        self.body = ctk.CTkFrame(self, fg_color="transparent")
        self.body.pack(fill="both", expand=True, padx=pad, pady=pad)


class StatCard(Card):
    """KPI tile with a coloured accent strip."""

    def __init__(self, master: Any, title: str, value: str = "—", caption: str = "",
                 accent: ThemeColor = PALETTE.primary) -> None:
        super().__init__(master)
        ctk.CTkFrame(self.body, height=4, corner_radius=2, fg_color=accent).pack(fill="x")
        ctk.CTkLabel(self.body, text=title.upper(), font=font(TYPO.size_caption, "bold"),
                     text_color=PALETTE.text_muted, anchor="w").pack(fill="x", pady=(8, 0))
        self._value = ctk.CTkLabel(self.body, text=value, font=font(TYPO.size_display, "bold"),
                                   text_color=PALETTE.text_primary, anchor="w")
        self._value.pack(fill="x")
        self._caption = ctk.CTkLabel(self.body, text=caption, font=font(TYPO.size_caption),
                                     text_color=PALETTE.text_muted, anchor="w")
        self._caption.pack(fill="x")

    def set_value(self, value: str, caption: Optional[str] = None) -> None:
        """Update the headline value and optional caption."""
        self._value.configure(text=value)
        if caption is not None:
            self._caption.configure(text=caption)


class StatusBadge(ctk.CTkLabel):
    """Pill-shaped status label coloured by semantic status."""

    def __init__(self, master: Any, status: str = "") -> None:
        super().__init__(master, text="", corner_radius=10, height=22, width=90,
                         font=font(TYPO.size_caption, "bold"), text_color=PALETTE.text_on_primary)
        self.set_status(status)

    def set_status(self, status: str) -> None:
        """Change text and colour."""
        self.configure(text=f"  {status}  ", fg_color=PALETTE.status_color(status))


class SectionHeader(ctk.CTkFrame):
    """Page heading with subtitle and a right-aligned ``actions`` slot."""

    def __init__(self, master: Any, title: str, subtitle: str = "") -> None:
        super().__init__(master, fg_color="transparent")
        self.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(self, text=title, font=font(TYPO.size_title, "bold"),
                     text_color=PALETTE.text_primary, anchor="w").grid(row=0, column=0, sticky="w")
        if subtitle:
            ctk.CTkLabel(self, text=subtitle, font=font(TYPO.size_caption),
                         text_color=PALETTE.text_muted, anchor="w").grid(row=1, column=0, sticky="w")
        self.actions = ctk.CTkFrame(self, fg_color="transparent", width=1, height=1)
        self.actions.grid(row=0, column=1, rowspan=2, sticky="e")


# --------------------------------------------------------------------------- #
# Form fields
# --------------------------------------------------------------------------- #
class LabeledEntry(ctk.CTkFrame):
    """Text field with caption and inline validation message."""

    def __init__(self, master: Any, label: str, placeholder: str = "", width: int = 200,
                 **entry_kwargs: Any) -> None:
        super().__init__(master, fg_color="transparent")
        ctk.CTkLabel(self, text=label, font=font(TYPO.size_caption, "bold"),
                     text_color=PALETTE.text_muted, anchor="w").pack(fill="x")
        self.entry = ctk.CTkEntry(self, placeholder_text=placeholder, width=width, height=36,
                                  corner_radius=10, border_color=PALETTE.border,
                                  font=font(TYPO.size_body), **entry_kwargs)
        self.entry.pack(fill="x", pady=(2, 0))
        self._error = ctk.CTkLabel(self, text="", font=font(TYPO.size_caption),
                                   text_color=PALETTE.danger, anchor="w", height=14)
        self._error.pack(fill="x")

    def get(self) -> str:
        """Current stripped text."""
        return self.entry.get().strip()

    def set(self, value: str) -> None:
        """Replace the text."""
        self.entry.delete(0, "end")
        if value:
            self.entry.insert(0, value)

    def clear(self) -> None:
        """Empty the field and its error."""
        self.set("")
        self.set_error("")

    def set_error(self, message: str) -> None:
        """Show (or clear with ``""``) a validation message."""
        self._error.configure(text=message, text_color=PALETTE.danger)
        self.entry.configure(border_color=PALETTE.danger if message else PALETTE.border)

    def set_hint(self, message: str) -> None:
        """Show a neutral helper message under the field."""
        self._error.configure(text=message, text_color=PALETTE.text_muted)
        self.entry.configure(border_color=PALETTE.border)

    def set_enabled(self, enabled: bool) -> None:
        """Toggle interactivity."""
        self.entry.configure(state="normal" if enabled else "disabled")


class PhoneEntry(LabeledEntry):
    """
    Phone field with a regex keystroke interceptor.

    A Tk ``validatecommand`` runs *before* every edit and rejects anything that
    would make the content differ from ``^[0-9]{0,10}$`` (letters, symbols,
    spaces, an 11th digit, pasted ``+91 …`` strings). :meth:`is_valid` then
    enforces the exact length ``^[0-9]{10}$`` on submit.
    """

    DIGITS = 10
    _PARTIAL = re.compile(r"^[0-9]{0,10}$")
    _COMPLETE = re.compile(r"^[0-9]{10}$")

    def __init__(self, master: Any, label: str = "Phone (10 digits)",
                 placeholder: str = "10-digit mobile number", width: int = 200,
                 required: bool = True) -> None:
        super().__init__(master, label, placeholder, width)
        self._placeholder = placeholder
        self._required = required
        intercept = (self.register(self._intercept_keystroke), "%P")
        self.entry.configure(validate="key", validatecommand=intercept)
        self.entry.bind("<KeyRelease>", lambda _e: self._show_progress(), add="+")

    def _intercept_keystroke(self, proposed: str) -> bool:
        """Tk validation hook: ``True`` accepts the edit, ``False`` vetoes it."""
        if proposed == self._placeholder or self._PARTIAL.match(proposed):
            return True
        self.bell()
        self.after_idle(lambda: self.set_error(f"Digits only — exactly {self.DIGITS} numbers."))
        return False

    def _show_progress(self) -> None:
        count = len(self.get())
        if count == 0:
            self.set_hint("")
        elif count < self.DIGITS:
            self.set_hint(f"{count}/{self.DIGITS} digits")
        else:
            self.set_hint("✓ 10 digits")

    def is_valid(self) -> bool:
        """Validate the final value, showing an inline error when invalid."""
        value = self.get()
        if not value:
            if self._required:
                self.set_error("Phone number is required.")
                return False
            self.set_error("")
            return True
        if not self._COMPLETE.match(value):
            self.set_error(f"Must be exactly {self.DIGITS} digits ({len(value)} entered).")
            return False
        self.set_error("")
        return True


def _mode_color(color: ThemeColor) -> str:
    """Resolve an adaptive ``(light, dark)`` colour for non-CTk widgets."""
    return color[1] if ctk.get_appearance_mode().lower() == "dark" else color[0]


class DatePickerField(ctk.CTkFrame):
    """
    Mouse-driven date selector.

    Shows the chosen date on a button; clicking it opens a modal
    :class:`tkcalendar.Calendar` window. A single click on a day selects it and
    closes the picker, so no manual date typing is ever required.
    """

    def __init__(self, master: Any, label: Optional[str] = "Date",
                 initial: Optional[date] = None, min_date: Optional[date] = None,
                 command: Optional[Callable[[date], None]] = None, width: int = 150) -> None:
        """
        :param label: Caption above the button (``None`` for a bare button).
        :param initial: Initially selected date (defaults to today).
        :param min_date: Earliest selectable date (e.g. today for bookings).
        :param command: Called with the new date after each selection.
        """
        super().__init__(master, fg_color="transparent")
        self._date = initial or date.today()
        self._min_date = min_date
        self._command = command
        self._popup: Optional[ctk.CTkToplevel] = None
        if label:
            ctk.CTkLabel(self, text=label, font=font(TYPO.size_caption, "bold"),
                         text_color=PALETTE.text_muted, anchor="w").pack(fill="x")
        self.button = ctk.CTkButton(self, text="", width=width, height=36, corner_radius=10,
                                    font=font(TYPO.size_body), fg_color=PALETTE.surface_alt,
                                    hover_color=PALETTE.border, text_color=PALETTE.text_primary,
                                    border_width=1, border_color=PALETTE.border, anchor="w",
                                    command=self.open_picker)
        self.button.pack(fill="x", pady=(2, 14) if label else 0)
        self._render()

    def _render(self) -> None:
        self.button.configure(text=f"  {self._date:%a, %d %b %Y}   ▾")

    def get_date(self) -> date:
        """Currently selected date."""
        return self._date

    def set_date(self, value: date, notify: bool = False) -> None:
        """Change the selection (optionally firing ``command``)."""
        self._date = value
        self._render()
        if notify and self._command:
            self._command(value)

    def set_min_date(self, value: Optional[date]) -> None:
        """Update the earliest selectable date."""
        self._min_date = value

    def open_picker(self) -> None:
        """Open the calendar pop-up next to the button."""
        if self._popup is not None and self._popup.winfo_exists():
            self._popup.focus_set()
            return
        try:
            from tkcalendar import Calendar
        except ImportError:
            from tkinter import messagebox
            messagebox.showerror("Date picker unavailable",
                                 "The 'tkcalendar' package is missing.\nRun: pip install tkcalendar")
            return

        popup = ctk.CTkToplevel(self)
        popup.title("Select date")
        popup.resizable(False, False)
        popup.transient(self.winfo_toplevel())
        popup.configure(fg_color=PALETTE.surface)
        self._popup = popup

        surface, text = _mode_color(PALETTE.surface), _mode_color(PALETTE.text_primary)
        muted, alt = _mode_color(PALETTE.text_muted), _mode_color(PALETTE.surface_alt)
        primary = _mode_color(PALETTE.primary)
        calendar = Calendar(
            popup, selectmode="day", year=self._date.year, month=self._date.month,
            day=self._date.day, mindate=self._min_date, firstweekday="monday",
            showweeknumbers=False, date_pattern="yyyy-mm-dd", cursor="hand2",
            font=(TYPO.family, 12), headersfont=(TYPO.family, 11, "bold"),
            background=primary, foreground="#FFFFFF", bordercolor=alt,
            headersbackground=alt, headersforeground=text,
            normalbackground=surface, normalforeground=text,
            weekendbackground=surface, weekendforeground=_mode_color(PALETTE.accent),
            othermonthbackground=alt, othermonthforeground=muted,
            othermonthwebackground=alt, othermonthweforeground=muted,
            selectbackground=_mode_color(PALETTE.success), selectforeground="#FFFFFF",
            disabledbackground=alt, disabledforeground=muted,
            disableddaybackground=alt, disableddayforeground=muted,
        )
        calendar.pack(padx=14, pady=(14, 8))
        calendar.bind("<<CalendarSelected>>", lambda _e: self._choose(calendar.selection_get()))

        bar = ctk.CTkFrame(popup, fg_color="transparent")
        bar.pack(fill="x", padx=14, pady=(0, 14))
        today = date.today()
        today_ok = self._min_date is None or today >= self._min_date
        make_button(bar, "Today", lambda: self._choose(today), "ghost", width=70,
                    state="normal" if today_ok else "disabled").pack(side="left")
        make_button(bar, "Cancel", self._close_popup, "secondary", width=80).pack(side="right")

        popup.update_idletasks()
        x = self.button.winfo_rootx()
        y = self.button.winfo_rooty() + self.button.winfo_height() + 4
        popup.geometry(f"+{x}+{y}")
        popup.protocol("WM_DELETE_WINDOW", self._close_popup)
        popup.bind("<Escape>", lambda _e: self._close_popup())
        popup.after(120, self._grab_popup)

    def _grab_popup(self) -> None:
        if self._popup is not None and self._popup.winfo_exists():
            try:
                self._popup.grab_set()
                self._popup.focus_set()
            except tk.TclError:  # window not viewable yet on some WMs
                pass

    def _choose(self, value: Any) -> None:
        if isinstance(value, date):
            self._close_popup()
            self.set_date(value, notify=True)

    def _close_popup(self) -> None:
        if self._popup is not None:
            try:
                self._popup.grab_release()
            except tk.TclError:
                pass
            self._popup.destroy()
            self._popup = None


class KeyedOptionMenu(ctk.CTkFrame):
    """Dropdown that maps human labels to underlying keys (e.g. ids)."""

    def __init__(self, master: Any, label: str, width: int = 200,
                 command: Optional[Callable[[Any], None]] = None) -> None:
        super().__init__(master, fg_color="transparent")
        self._items: List[Tuple[Any, str]] = []
        self._command = command
        ctk.CTkLabel(self, text=label, font=font(TYPO.size_caption, "bold"),
                     text_color=PALETTE.text_muted, anchor="w").pack(fill="x")
        self.menu = ctk.CTkOptionMenu(self, values=["—"], width=width, height=36, corner_radius=10,
                                      font=font(TYPO.size_body), dynamic_resizing=False,
                                      fg_color=PALETTE.surface_alt, button_color=PALETTE.primary,
                                      button_hover_color=PALETTE.primary_hover,
                                      text_color=PALETTE.text_primary,
                                      command=self._on_change)
        self.menu.pack(fill="x", pady=(2, 14))

    def set_items(self, items: Sequence[Tuple[Any, str]], keep_selection: bool = True) -> None:
        """Replace options with ``[(key, label), ...]``."""
        previous = self.selected_key if keep_selection else None
        self._items = list(items)
        labels = [label for _, label in self._items] or ["—"]
        self.menu.configure(values=labels)
        keys = [k for k, _ in self._items]
        self.select_key(previous if previous in keys else (keys[0] if keys else None))

    def select_key(self, key: Any) -> None:
        """Programmatically select the option whose key equals ``key``."""
        for k, label in self._items:
            if k == key:
                self.menu.set(label)
                return
        self.menu.set("—")

    @property
    def selected_key(self) -> Any:
        """Key of the currently selected option, or ``None``."""
        current = self.menu.get()
        return next((k for k, label in self._items if label == current), None)

    def _on_change(self, _label: str) -> None:
        if self._command:
            self._command(self.selected_key)


# --------------------------------------------------------------------------- #
# Layout containers
# --------------------------------------------------------------------------- #
class ResponsiveGrid(ctk.CTkFrame):
    """
    Container that re-flows its children into ``1..max_columns`` equal columns
    based on its current width (``width // min_item_width``).
    """

    def __init__(self, master: Any, min_item_width: int = 280, max_columns: int = 4,
                 gap: int = LAYOUT.padding_md) -> None:
        super().__init__(master, fg_color="transparent")
        self._min_item_width = min_item_width
        self._max_columns = max_columns
        self._gap = gap
        self._items: List[Tuple[ctk.CTkBaseClass, int]] = []
        self._columns = 0
        self.bind("<Configure>", self._on_configure, add="+")

    def add(self, widget: Any, span: int = 1) -> Any:
        """Register a child (created with this grid as master); returns it."""
        self._items.append((widget, span))
        self._relayout(force=True)
        return widget

    def clear(self) -> None:
        """Destroy all registered children."""
        for widget, _ in self._items:
            widget.destroy()
        self._items.clear()

    def _on_configure(self, event: tk.Event) -> None:
        self._relayout(width=event.width)

    def _relayout(self, width: Optional[int] = None, force: bool = False) -> None:
        width = width or self.winfo_width()
        scaling = ctk.ScalingTracker.get_widget_scaling(self) if width > 1 else 1.0
        columns = max(1, min(self._max_columns, int(width / scaling) // self._min_item_width)) \
            if width > 1 else 1
        if columns == self._columns and not force:
            return
        for index in range(max(self._columns, columns, 1)):
            self.grid_columnconfigure(index, weight=1 if index < columns else 0,
                                      uniform="rgrid" if index < columns else "")
        self._columns = columns
        row = col = 0
        half = self._gap // 2
        for widget, span in self._items:
            span = min(span, columns)
            if col + span > columns:
                row, col = row + 1, 0
            widget.grid(row=row, column=col, columnspan=span, sticky="nsew", padx=half, pady=half)
            col += span
            if col >= columns:
                row, col = row + 1, 0


# --------------------------------------------------------------------------- #
# Data table
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ColumnSpec:
    """Column definition for :class:`DataTable`."""

    key: str
    title: str
    weight: int = 1
    anchor: str = "w"
    #: 1 = always visible; 2 = hidden in compact (mobile) mode.
    priority: int = 1
    formatter: Optional[Callable[[Any], str]] = None


class DataTable(ctk.CTkFrame):
    """Striped, selectable table rendered on a single Tk grid for perfect alignment."""

    MAX_ROWS = 250

    def __init__(self, master: Any, columns: Sequence[ColumnSpec], key: str,
                 on_select: Optional[Callable[[Dict[str, Any]], None]] = None,
                 row_text_color: Optional[Callable[[Dict[str, Any]], Optional[ThemeColor]]] = None,
                 empty_text: str = "No records to display.") -> None:
        super().__init__(master, fg_color="transparent")
        self._columns = list(columns)
        self._key = key
        self._on_select = on_select
        self._row_text_color = row_text_color
        self._empty_text = empty_text
        self._rows: List[Dict[str, Any]] = []
        self._cells: Dict[Any, List[ctk.CTkLabel]] = {}
        self._selected: Any = None
        self._compact = False

    @property
    def visible_columns(self) -> List[ColumnSpec]:
        """Columns shown in the current density mode."""
        return [c for c in self._columns if not (self._compact and c.priority > 1)]

    @property
    def selected_key(self) -> Any:
        """Key of the selected row, or ``None``."""
        return self._selected

    def set_compact(self, compact: bool) -> None:
        """Hide low-priority columns (mobile) or show all (desktop)."""
        if compact != self._compact:
            self._compact = compact
            self._render()

    def set_rows(self, rows: Sequence[Dict[str, Any]]) -> None:
        """Replace table data, preserving the selection when possible."""
        self._rows = list(rows)[: self.MAX_ROWS]
        if self._selected not in {r[self._key] for r in self._rows}:
            self._selected = None
        self._render()

    def _render(self) -> None:
        for child in self.winfo_children():
            child.destroy()
        self._cells.clear()
        cols = self.visible_columns
        for index in range(len(self._columns)):
            self.grid_columnconfigure(index, weight=0, uniform="")
        for index, col in enumerate(cols):
            self.grid_columnconfigure(index, weight=col.weight, uniform="dtable")
            ctk.CTkLabel(self, text=col.title.upper(), anchor=col.anchor, height=30,
                         font=font(TYPO.size_caption, "bold"), text_color=PALETTE.text_muted,
                         fg_color=PALETTE.surface_alt, corner_radius=0).grid(
                row=0, column=index, sticky="nsew")
        if not self._rows:
            ctk.CTkLabel(self, text=self._empty_text, text_color=PALETTE.text_muted,
                         font=font(TYPO.size_body), height=48).grid(
                row=1, column=0, columnspan=max(1, len(cols)), sticky="nsew")
            return
        for r_index, row in enumerate(self._rows, start=1):
            key = row[self._key]
            color = (self._row_text_color(row) if self._row_text_color else None) or PALETTE.text_primary
            labels: List[ctk.CTkLabel] = []
            for c_index, col in enumerate(cols):
                value = row.get(col.key, "")
                text = col.formatter(value) if col.formatter else ("" if value is None else str(value))
                label = ctk.CTkLabel(self, text=text, anchor=col.anchor, height=32, corner_radius=0,
                                     font=font(TYPO.size_body), text_color=color,
                                     fg_color=self._row_bg(r_index, key))
                label.grid(row=r_index, column=c_index, sticky="nsew")
                label.bind("<Button-1>", lambda _e, k=key: self._select(k))
                labels.append(label)
            self._cells[key] = labels

    def _row_bg(self, index: int, key: Any) -> ThemeColor:
        if key == self._selected:
            return ("#DBEAFE", "#1E3A8A")
        return PALETTE.surface if index % 2 else ("#F8FAFC", "#0F172A")

    def _select(self, key: Any) -> None:
        previous, self._selected = self._selected, key
        for index, row in enumerate(self._rows, start=1):
            k = row[self._key]
            if k in (previous, key) and k in self._cells:
                for label in self._cells[k]:
                    label.configure(fg_color=self._row_bg(index, k))
        if self._on_select:
            selected: Optional[dict[str, Any]] = next(
                (r for r in self._rows if r[self._key] == key), None
            )
            if selected is not None:
                self._on_select(selected)


# --------------------------------------------------------------------------- #
# Feedback
# --------------------------------------------------------------------------- #
class NotificationBar(ctk.CTkFrame):
    """Transient, colour-coded status banner."""

    _LEVEL_COLORS: Dict[str, ThemeColor] = {
        "info": PALETTE.primary, "success": PALETTE.success,
        "warning": PALETTE.warning, "error": PALETTE.danger,
    }

    def __init__(self, master: Any) -> None:
        super().__init__(master, fg_color="transparent", height=34, corner_radius=10)
        self._label = ctk.CTkLabel(self, text="", font=font(TYPO.size_body, "bold"),
                                   text_color=PALETTE.text_on_primary, anchor="w")
        self._label.pack(fill="both", expand=True, padx=12, pady=4)
        self._job: Optional[str] = None

    def show(self, message: str, level: str = "info", duration_ms: int = 4500) -> None:
        """Display ``message``; it auto-hides after ``duration_ms``."""
        if self._job:
            self.after_cancel(self._job)
        self.configure(fg_color=self._LEVEL_COLORS.get(level, PALETTE.primary))
        self._label.configure(text=message)
        self._job = self.after(duration_ms, self.hide)

    def hide(self) -> None:
        """Clear the banner."""
        self._job = None
        self.configure(fg_color="transparent")
        self._label.configure(text="")
        