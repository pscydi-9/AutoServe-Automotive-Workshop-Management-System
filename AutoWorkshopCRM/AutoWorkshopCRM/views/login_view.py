"""
views.login_view
================

Responsive two-step sign-in screen.

Step 1 — username + password (+ choice of OTP channel).
Step 2 — 6-digit one-time code with live expiry countdown and resend cooldown.

* Desktop/tablet: brand hero panel on the left, form card on the right.
* Mobile (Pydroid 3 / narrow windows): hero collapses; the form fills the width.

The view only renders state and forwards intents to
:class:`controllers.auth_controller.AuthController`. Password hashing and OTP
delivery run on a worker pool; results are marshalled back onto the Tk thread,
so the window stays responsive during bcrypt work and SMTP handshakes.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

import customtkinter as ctk

from controllers.auth_controller import AuthController, OTPDispatchInfo
from controllers.base_controller import OperationResult
from core.config import AppConfig
from models.auth_model import OTPChannel, Session
from views.components import (
    LAYOUT,
    PALETTE,
    TYPO,
    Card,
    LabeledEntry,
    LayoutMode,
    font,
    make_button,
)


class LoginView(ctk.CTkFrame):
    """Password + OTP login screen."""

    _CHANNELS = {"Email": OTPChannel.EMAIL, "SMS": OTPChannel.SMS}

    def __init__(self, master: Any, controller: AuthController, config: AppConfig,
                 on_authenticated: Callable[[Session], None]) -> None:
        """
        :param master: Parent widget (the application root).
        :param controller: Authentication controller.
        :param config: Application configuration (branding, security policy).
        :param on_authenticated: Invoked with the new session after successful verification.
        """
        super().__init__(master, fg_color=PALETTE.background, corner_radius=0)
        self._controller = controller
        self._config = config
        self._on_authenticated = on_authenticated
        self._cooldown: int = 0
        self._tick_job: Optional[str] = None
        self._mode: Optional[LayoutMode] = None

        self.grid_rowconfigure(0, weight=1)
        self._hero = self._build_hero()
        self._form_host = ctk.CTkFrame(self, fg_color="transparent")
        self._form_host.grid_rowconfigure(0, weight=1)
        self._form_host.grid_columnconfigure(0, weight=1)
        self._card = self._build_form(self._form_host)
        self._card.grid(row=0, column=0)
        self._show_step(1)
        self.apply_layout(LayoutMode.DESKTOP)
        self.after(50, self._password.entry.focus_set)

    # ------------------------------------------------------------------ #
    # Construction
    # ------------------------------------------------------------------ #
    def _build_hero(self) -> ctk.CTkFrame:
        hero = ctk.CTkFrame(self, fg_color=PALETTE.primary, corner_radius=0)
        inner = ctk.CTkFrame(hero, fg_color="transparent")
        inner.place(relx=0.5, rely=0.5, anchor="center", relwidth=0.8)
        white = PALETTE.text_on_primary
        ctk.CTkLabel(inner, text="AutoServe Pro", font=font(34, "bold"), text_color=white,
                     anchor="w").pack(fill="x")
        ctk.CTkLabel(inner, text="Workshop CRM & Service Planner", font=font(TYPO.size_title),
                     text_color=white, anchor="w").pack(fill="x", pady=(0, 24))
        for line in ("Real-time job scheduling board",
                     "ACID-safe spare-part inventory",
                     "Automatic itemised billing",
                     "bcrypt passwords + OTP two-factor sign-in"):
            ctk.CTkLabel(inner, text=f"✓  {line}", font=font(TYPO.size_heading), text_color=white,
                         anchor="w").pack(fill="x", pady=4)
        ctk.CTkLabel(inner, text=f"v{self._config.version}", font=font(TYPO.size_caption),
                     text_color=white, anchor="w").pack(fill="x", pady=(32, 0))
        return hero

    def _build_form(self, master: Any) -> Card:
        card = Card(master, title="Secure sign-in")
        body = card.body
        self._step_label = ctk.CTkLabel(body, text="", font=font(TYPO.size_caption, "bold"),
                                        text_color=PALETTE.primary, anchor="w")
        self._step_label.pack(fill="x", pady=(0, 8))

        # -- Step 1: credentials ------------------------------------------
        self._step1 = ctk.CTkFrame(body, fg_color="transparent")
        self._username = LabeledEntry(self._step1, "Username", "admin", width=320)
        self._username.set(self._controller.default_username)
        self._username.pack(fill="x")
        self._password = LabeledEntry(self._step1, "Password", "Your password", width=320, show="•")
        self._password.pack(fill="x")
        self._show_pw = ctk.CTkCheckBox(self._step1, text="Show password", font=font(TYPO.size_caption),
                                        command=self._toggle_password, checkbox_width=18,
                                        checkbox_height=18)
        self._show_pw.pack(anchor="w", pady=(0, 10))
        for field in (self._username, self._password):
            field.entry.bind("<Return>", lambda _e: self._on_continue())

        ctk.CTkLabel(self._step1, text="SEND ONE-TIME CODE VIA", font=font(TYPO.size_caption, "bold"),
                     text_color=PALETTE.text_muted, anchor="w").pack(fill="x")
        self._channel = ctk.CTkSegmentedButton(self._step1, values=list(self._CHANNELS), height=34,
                                               font=font(TYPO.size_body, "bold"),
                                               selected_color=PALETTE.primary,
                                               selected_hover_color=PALETTE.primary_hover)
        self._channel.set("Email")
        self._channel.pack(fill="x", pady=(2, 12))
        self._continue_btn = make_button(self._step1, "Continue", self._on_continue, height=40)
        self._continue_btn.pack(fill="x")
        hint = self._controller.demo_credentials_hint
        if hint:
            ctk.CTkLabel(self._step1, text=f"{hint}  (change it in Admin Settings)",
                         font=font(TYPO.size_caption), text_color=PALETTE.text_muted,
                         anchor="w").pack(fill="x", pady=(8, 0))

        # -- Step 2: one-time code ----------------------------------------
        self._step2 = ctk.CTkFrame(body, fg_color="transparent")
        self._sent_to = ctk.CTkLabel(self._step2, text="", font=font(TYPO.size_body), wraplength=320,
                                     justify="left", anchor="w", text_color=PALETTE.text_primary)
        self._sent_to.pack(fill="x", pady=(0, 8))
        self._otp = LabeledEntry(self._step2, f"{self._controller.otp_length}-digit code",
                                 "• • • • • •", width=320, justify="center")
        self._otp.entry.configure(font=font(TYPO.size_title, "bold"))
        self._otp.pack(fill="x")
        self._otp.entry.bind("<Return>", lambda _e: self._on_verify())
        self._countdown = ctk.CTkLabel(self._step2, text="", font=font(TYPO.size_caption),
                                       text_color=PALETTE.text_muted, anchor="w")
        self._countdown.pack(fill="x")
        self._verify_btn = make_button(self._step2, "Verify & sign in", self._on_verify,
                                       variant="success", height=40)
        self._verify_btn.pack(fill="x", pady=(6, 4))
        actions = ctk.CTkFrame(self._step2, fg_color="transparent")
        actions.pack(fill="x")
        self._back_btn = make_button(actions, "← Back", self._on_back, variant="ghost", width=80,
                                     height=30)
        self._back_btn.pack(side="left")
        self._resend_btn = make_button(actions, "Resend code", self._on_resend, variant="ghost",
                                       width=160, height=30, state="disabled")
        self._resend_btn.pack(side="right")

        self._status = ctk.CTkLabel(body, text="", font=font(TYPO.size_body), wraplength=320,
                                    justify="left", anchor="w")
        self._status.pack(side="bottom", fill="x", pady=(8, 0))
        return card

    def _show_step(self, step: int) -> None:
        self._step1.pack_forget()
        self._step2.pack_forget()
        if step == 1:
            self._step_label.configure(text="STEP 1 OF 2 — VERIFY YOUR PASSWORD")
            self._step1.pack(fill="x")
        else:
            self._step_label.configure(text="STEP 2 OF 2 — ENTER THE ONE-TIME CODE")
            self._step2.pack(fill="x")

    # ------------------------------------------------------------------ #
    # Responsiveness
    # ------------------------------------------------------------------ #
    def apply_layout(self, mode: LayoutMode) -> None:
        """Re-arrange for ``mode`` (called by the application on resize)."""
        if mode == self._mode:
            return
        self._mode = mode
        self._hero.grid_forget()
        self._form_host.grid_forget()
        if mode is LayoutMode.MOBILE:
            self.grid_columnconfigure(0, weight=1, uniform="")
            self.grid_columnconfigure(1, weight=0, uniform="")
            self._form_host.grid(row=0, column=0, sticky="nsew", padx=LAYOUT.padding_sm,
                                 pady=LAYOUT.padding_sm)
            self._card.grid_configure(sticky="ew")
        else:
            hero_weight = 1 if mode is LayoutMode.DESKTOP else 2
            self.grid_columnconfigure(0, weight=hero_weight, uniform="login")
            self.grid_columnconfigure(1, weight=1 if mode is LayoutMode.DESKTOP else 3,
                                      uniform="login")
            self._hero.grid(row=0, column=0, sticky="nsew")
            self._form_host.grid(row=0, column=1, sticky="nsew", padx=LAYOUT.padding_lg)
            self._card.grid_configure(sticky="")

    # ------------------------------------------------------------------ #
    # Event handlers
    # ------------------------------------------------------------------ #
    def _set_status(self, message: str, level: str = "info") -> None:
        colors = {"info": PALETTE.text_muted, "success": PALETTE.success, "error": PALETTE.danger}
        self._status.configure(text=message, text_color=colors.get(level, PALETTE.text_muted))

    def _toggle_password(self) -> None:
        self._password.entry.configure(show="" if self._show_pw.get() else "•")

    def _on_continue(self) -> None:
        username, password = self._username.get(), self._password.entry.get()
        self._username.set_error("" if username else "Required")
        self._password.set_error("" if password else "Required")
        if not username or not password:
            return
        self._continue_btn.configure(state="disabled", text="Verifying…")
        self._set_status("Checking your password and sending the code in the background…")
        self._controller.begin_login(username, password, self._CHANNELS[self._channel.get()],
                                     self._on_code_sent)

    def _on_code_sent(self, result: OperationResult[OTPDispatchInfo]) -> None:
        if not self.winfo_exists():
            return
        self._continue_btn.configure(state="normal", text="Continue")
        if not result.success or result.data is None:
            self._set_status(result.message, "error")
            return
        self._password.clear()
        self._show_step(2)
        self._apply_dispatch(result)

    def _apply_dispatch(self, result: OperationResult[OTPDispatchInfo]) -> None:
        info = result.data
        if info is None:
            return
        self._cooldown = info.resend_cooldown_seconds
        self._sent_to.configure(text=f"We sent a {self._controller.otp_length}-digit code to "
                                     f"{info.destination_masked} ({info.channel.value.upper()}).")
        self._otp.clear()
        self._otp.set_enabled(True)
        self._otp.entry.focus_set()
        self._verify_btn.configure(state="normal")
        hint = ("Mock mode: the code is printed in the terminal as a JSON diagnostic block."
                if info.is_mock else "Check your inbox (and spam folder).")
        self._set_status(hint, "success")
        self._start_ticker()

    def _on_verify(self) -> None:
        code = self._otp.get()
        length = self._controller.otp_length
        if len(code) != length or not code.isdigit():
            self._otp.set_error(f"Enter the {length}-digit code.")
            return
        self._otp.set_error("")
        result = self._controller.verify_login_code(code)
        if result.success and result.data is not None:
            self._stop_ticker()
            self._on_authenticated(result.data)
            return
        self._set_status(result.message, "error")
        if self._controller.login_seconds_remaining() == 0:
            self._verify_btn.configure(state="disabled")

    def _on_resend(self) -> None:
        self._resend_btn.configure(state="disabled", text="Sending…")
        self._controller.resend_login_code(self._CHANNELS[self._channel.get()], self._on_resent)

    def _on_resent(self, result: OperationResult[OTPDispatchInfo]) -> None:
        if not self.winfo_exists():
            return
        if not result.success:
            self._set_status(result.message, "error")
            self._resend_btn.configure(state="normal", text="Resend code")
            if "password" in result.message.lower():
                self._on_back()
            return
        self._apply_dispatch(result)

    def _on_back(self) -> None:
        self._stop_ticker()
        self._controller.cancel_login()
        self._countdown.configure(text="")
        self._show_step(1)
        self._password.entry.focus_set()

    # ------------------------------------------------------------------ #
    # Countdown
    # ------------------------------------------------------------------ #
    def _start_ticker(self) -> None:
        self._stop_ticker()
        self._tick()

    def _stop_ticker(self) -> None:
        if self._tick_job:
            self.after_cancel(self._tick_job)
            self._tick_job = None

    def _tick(self) -> None:
        remaining = self._controller.login_seconds_remaining()
        self._cooldown = max(0, self._cooldown - 1)
        if remaining > 0:
            self._countdown.configure(text=f"Code expires in {remaining // 60}:{remaining % 60:02d}",
                                      text_color=PALETTE.warning if remaining < 60 else PALETTE.text_muted)
        else:
            self._countdown.configure(text="Code expired — request a new one.",
                                      text_color=PALETTE.danger)
            self._verify_btn.configure(state="disabled")
        self._resend_btn.configure(
            state="normal" if self._cooldown == 0 else "disabled",
            text="Resend code" if self._cooldown == 0 else f"Resend code in {self._cooldown}s")
        if remaining > 0 or self._cooldown > 0:
            self._tick_job = self.after(1000, self._tick)
        else:
            self._tick_job = None

    def destroy(self) -> None:
        """Cancel timers before the widget tree is torn down."""
        self._stop_ticker()
        super().destroy()
