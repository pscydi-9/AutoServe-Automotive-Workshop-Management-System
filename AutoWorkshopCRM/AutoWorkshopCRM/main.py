from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.config import AppConfig, get_config  # noqa: E402
from core.db_manager import DatabaseManager  # noqa: E402
from core.notification_service import NotificationService  # noqa: E402
from models.auth_model import AuthModel  # noqa: E402
from models.workshop_model import WorkshopModel  # noqa: E402

logger = logging.getLogger("autoserve")


def configure_logging() -> None:
    """Console logging with timestamps and thread names."""
    logging.basicConfig(
        level=os.environ.get("WORKSHOP_LOG_LEVEL", "INFO"),
        format="%(asctime)s | %(levelname)-7s | %(threadName)-16s | %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def is_mobile_runtime() -> bool:
    """Detect Android/Pydroid 3 execution."""
    return any(key in os.environ for key in ("ANDROID_ROOT", "ANDROID_DATA", "ANDROID_ARGUMENT"))


def describe_email_mode(config: AppConfig) -> str:
    """Human-readable OTP e-mail mode (never reveals the App Password)."""
    if config.smtp.mock_mode:
        return "MOCK (codes printed to this terminal; set EMAIL_SENDER + EMAIL_APP_PASSWORD for Gmail)"
    return f"LIVE via {config.smtp.host}:{config.smtp.port} as {config.smtp.username}"


def bootstrap_data_layer(config: AppConfig) -> tuple[DatabaseManager, AuthModel, WorkshopModel]:
    """
    Create the database, migrate/apply the schema, seed data (UI-independent).

    The :class:`AuthModel` receives the synchronous e-mail/SMS transports and
    runs them on its own ``ThreadPoolExecutor``; the bcrypt-hashed seed admin
    is created on first launch (credentials from ``ADMIN_*`` variables).
    """
    db = DatabaseManager(config.database)
    db.initialize_schema()
    notifier = NotificationService(config.smtp, config.sms)
    auth_model = AuthModel(db, config.security, notifier,
                           app_name=config.app_name.split("—")[0].strip())
    auth_model.ensure_admin(config.admin)
    workshop_model = WorkshopModel(db, config.business)
    workshop_model.seed_demo_data()
    return db, auth_model, workshop_model


def run_headless_check(config: AppConfig) -> int:
    """Smoke-test the data layer without a display; returns a process exit code."""
    db, auth, workshop = bootstrap_data_layer(config)
    try:
        admin = auth.find_by_username(config.admin.username)
        print(f"Database      : {config.database.path}")
        print(f"Schema version: {db.schema_version()}  healthy={db.health_check()}")
        print(f"Vehicles      : {len(workshop.list_vehicles())}")
        print(f"Catalogue     : {len(workshop.list_services())} services")
        print(f"Admin account : {admin.username + ' <' + admin.email + '>' if admin else 'n/a'}")
        print(f"OTP e-mail    : {describe_email_mode(config)}")
        print(f"Metrics       : {workshop.dashboard_metrics()}")
        return 0
    finally:
        auth.shutdown()
        db.close()


class WorkshopApplication:
    """Owns the Tk root and swaps between login and dashboard views."""

    def __init__(self, config: AppConfig) -> None:
        import customtkinter as ctk

        from controllers.auth_controller import AuthController
        from controllers.workshop_controller import WorkshopController
        from views.components import LayoutMode, MainThreadDispatcher

        self._ctk = ctk
        self._layout_mode_cls = LayoutMode
        self.config = config

        ctk.set_appearance_mode(config.appearance_mode)
        ctk.set_default_color_theme("blue")
        self.root = ctk.CTk(fg_color=config.theme.background)
        self.root.title(config.app_name)
        self.root.minsize(config.layout.min_width, config.layout.min_height)
        
        # --- INITIAL WINDOW STATUS TRACKING ---
        self._is_fullscreen = False
        
        # --- TOGGLEABLE WINDOW CONTROLS BINDING ---
        self.root.bind("<F11>", lambda event: self.toggle_fullscreen())
        self.root.bind("<Escape>", lambda event: self.shutdown())
        
        self._configure_geometry()

        self.db, self.auth_model, workshop_model = bootstrap_data_layer(config)
        self.dispatcher = MainThreadDispatcher(self.root)
        
        # Correctly mapped parameters matching your system layout
        self.auth_controller = AuthController(self.auth_model, self.dispatcher.dispatch, config)
        self.workshop_controller = WorkshopController(workshop_model, config.business)

        self._view: Optional[object] = None
        self._mode: Optional[LayoutMode] = None
        self._resize_job: Optional[str] = None
        self.root.bind("<Configure>", self._on_configure, add="+")
        self.root.protocol("WM_DELETE_WINDOW", self.shutdown)
        self.show_login()


    # -- window configurations ------------------------------------------ #
    def _configure_geometry(self) -> None:
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        if is_mobile_runtime() or screen_w < self.config.layout.mobile_breakpoint:
            self._ctk.set_widget_scaling(self.config.layout.mobile_widget_scaling)
            self.root.geometry(f"{screen_w}x{screen_h}+0+0")
        else:
            self.root.geometry(self.config.layout.default_geometry)

    def toggle_fullscreen(self) -> None:
        """Dynamically toggle between borderless full screen and a standard window."""
        self._is_fullscreen = not self._is_fullscreen
        
        if self._is_fullscreen:
            # Switch to pure borderless full screen edge-to-edge
            self.root.overrideredirect(True)
            screen_w = self.root.winfo_screenwidth()
            screen_h = self.root.winfo_screenheight()
            self.root.geometry(f"{screen_w}x{screen_h}+0+0")
            self.root.state('zoomed')
        else:
            # Return to a standard window layout with title bar controls
            self.root.overrideredirect(False)
            self.root.state('normal')
            self.root.geometry(self.config.layout.default_geometry)
            
        # Re-apply layouts instantly to adjust the interface elements
        self._apply_layout()

    def _on_configure(self, event: object) -> None:
        if getattr(event, "widget", None) is not self.root:
            return
        root = self.root
        if root is None:
            return
        if self._resize_job:
            root.after_cancel(self._resize_job)
        self._resize_job = root.after(80, self._apply_layout)

    def _apply_layout(self) -> None:
        self._resize_job = None
        scaling = self._ctk.ScalingTracker.get_window_scaling(self.root)
        mode = self._layout_mode_cls.from_width(int(self.root.winfo_width() / scaling))
        if mode != self._mode:
            self._mode = mode
            if self._view is not None:
                self._view.apply_layout(mode)  # type: ignore[attr-defined]

    def _swap(self, view: object) -> None:
        if self._view is not None:
            self._view.destroy()  # type: ignore[attr-defined]
        self._view = view
        view.pack(fill="both", expand=True)  # type: ignore[attr-defined]
        self._mode = None
        self.root.after(10, self._apply_layout)

    # -- navigation ---------------------------------------------------- #
    def show_login(self) -> None:
        """Display the OTP login screen."""
        from views.login_view import LoginView
        self._swap(LoginView(self.root, self.auth_controller, self.config, self._on_authenticated))

    def _on_authenticated(self, session: object) -> None:
        from views.dashboard_view import DashboardView
        logger.info("User %s signed in.", session.user.email)  # type: ignore[attr-defined]
        self._swap(DashboardView(self.root, self.workshop_controller, self.auth_controller,
                                 session, self.config, self._on_logout))

    def _on_logout(self) -> None:
        self.auth_controller.logout()
        self.show_login()

    # -- lifecycle ----------------------------------------------------- #
    def run(self) -> None:
        """Enter the Tk main loop."""
        self.root.mainloop()

    def shutdown(self) -> None:
        """Release threads, connections and the window."""
        logger.info("Shutting down…")
        self.dispatcher.stop()
        self.notifier.shutdown(wait=False)
        self.db.close()
        self.root.destroy()



def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="AutoServe Pro workshop CRM")
    parser.add_argument("--check", action="store_true", help="headless data-layer smoke test")
    parser.add_argument("--reset", action="store_true", help="delete the local database first")
    args = parser.parse_args(argv)

    configure_logging()
    config = get_config()
    if args.reset and config.database.path.exists():
        for suffix in ("", "-wal", "-shm"):
            Path(f"{config.database.path}{suffix}").unlink(missing_ok=True)
        logger.info("Database reset.")
    if args.check:
        return run_headless_check(config)

    logger.info("OTP e-mail mode: %s", describe_email_mode(config))
    app = WorkshopApplication(config)
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
