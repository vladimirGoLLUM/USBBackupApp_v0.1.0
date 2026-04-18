"""Main window controller extracted from entrypoint.

Contains `BackupApp` with all UI state and user workflows.
"""

import json
import threading
import time
from datetime import datetime
from pathlib import Path
from tkinter import END, StringVar, filedialog, messagebox, font as tkfont

import customtkinter as ctk

from core.app_paths import PROJECT_ROOT, read_version, resolve_app_state_path
from core.backup_logic import BackupLogic
from services.autolaunch_service import AutoLaunchService
from services.scan_service import ScanService
from services.transfer_service import TransferService
from device_detector import DeviceDetector
from ui.progress_presenter import ProgressPresenter
from utils.i18n import LanguageManager

APP_STATE_PATH = resolve_app_state_path()


def _normalize_autostart_device_id(raw: str) -> str:
    try:
        v = (raw or "").strip()
        if not v:
            return ""
        if "_" in v:
            tail = v.rsplit("_", 1)[-1].strip()
            if tail.isdigit():
                return tail
        if v.isdigit():
            return v
        return v
    except Exception:
        return (raw or "").strip()


class BackupApp:
    """Main customtkinter window and user workflow coordinator."""

    FEEDBACK_EMAIL = "pycraft-dev@21051992.ru"

    BG_COLOR = "#1a1a1a"
    PANEL_COLOR = "#202020"
    PANEL_ALT_COLOR = "#252525"
    BORDER_COLOR = "#2f2f2f"
    TEXT_COLOR = "#f5f5f5"
    MUTED_TEXT_COLOR = "#b3b3b3"
    ACCENT_COLOR = "#3b82f6"
    ACCENT_HOVER_COLOR = "#2563eb"
    DISABLED_COLOR = "#3a3a3a"
    LOG_SYSTEM_COLOR = "#9ca3af"
    LOG_SUCCESS_COLOR = "#22c55e"
    LOG_ERROR_COLOR = "#ef4444"

    def __init__(
        self,
        root,
        autostart_device_id: str | None = None,
        autostart_volume_label: str | None = None,
    ) -> None:
        self.root = root
        ctk.set_appearance_mode("dark")
        self.version = read_version()
        self.root.geometry("1140x760")
        self.root.minsize(1020, 700)
        self.root.configure(fg_color=self.BG_COLOR)
        self.devices = []
        self.analysis = None
        self.is_busy = False
        self.laconic_log = True
        self.log_autofollow = True

        self.language_manager = LanguageManager()
        raw_state = self._read_first_app_state()
        if raw_state:
            lang = str(raw_state.get("language", "ru")).strip().lower()
            if lang in LanguageManager.SUPPORTED:
                self.language_manager.set_language(lang)

        self.selected_device = StringVar()
        self.backup_target_dir = StringVar()
        if raw_state:
            last_backup_path = str(raw_state.get("last_backup_path", "")).strip()
            if last_backup_path:
                self.backup_target_dir.set(last_backup_path)

        self.status = StringVar(value=self._tr("status_pick_device_and_backup"))
        self.progress_text = StringVar(value=self._tr("progress_zero"))
        self.time_text = StringVar(value=self._tr("time_initial"))
        self.progress_detail_text = StringVar(value=self._tr("file_dash"))
        self.progress_speed_text = StringVar(value=self._tr("speed_zero"))
        self.active_operation = None

        self.transfer_cancel_requested = False
        self.transfer_cancel_event = None

        self.scan_cancel_event = None
        self.last_progress_update = 0.0

        # Auto-launch / auto-scan (started by Task Scheduler)
        self.autostart_device_id = (autostart_device_id or "").strip()
        self.autostart_volume_label = (autostart_volume_label or "").strip()
        self._autostart_retry_left = 0

        # Task Scheduler one-click setup UI
        self.autolaunch_status_var = StringVar(value=self._tr("autolaunch_status_off"))

        self._build_ui()
        self.root.title(self._tr("window_title", version=self.version))
        self.selected_device.trace_add("write", lambda *_: self._on_inputs_changed())
        self.backup_target_dir.trace_add("write", lambda *_: self._on_inputs_changed())
        self.backup_target_dir.trace_add("write", lambda *_: self._save_app_state())
        self.refresh_devices()
        self._refresh_autolaunch_status_from_system()

        # If started with autostart args, keep retrying for a short period.
        # On some systems Task Scheduler triggers before the drive is fully mounted.
        if self.autostart_device_id:
            # Show that this run was started by autolaunch.
            self.autolaunch_status_var.set(self._tr("autolaunch_status_on"))
            self._autostart_retry_left = 15
            self.root.after(900, self._maybe_autostart_scan)

    def _tr(self, key: str, **kwargs) -> str:
        """Возвращает локализованную строку по ключу."""
        return self.language_manager.get(key, **kwargs)

    @staticmethod
    def _read_first_app_state() -> dict | None:
        """Читает первый доступный файл состояния (основной или legacy)."""
        legacy_path = PROJECT_ROOT / "config" / "app_state.json"
        candidates = [APP_STATE_PATH]
        if legacy_path != APP_STATE_PATH:
            candidates.append(legacy_path)
        for state_path in candidates:
            if not state_path.exists():
                continue
            try:
                return json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
        return None

    def _size_units(self) -> tuple[str, str, str, str]:
        """Подписи единиц размера для ProgressPresenter."""
        return (
            self._tr("unit_b"),
            self._tr("unit_kb"),
            self._tr("unit_mb"),
            self._tr("unit_gb"),
        )

    def _localize_scan_service_message(self, raw: str) -> str:
        """Переводит известные служебные сообщения сканирования без изменения сервиса."""
        if raw == "На флешке нет файлов для бэкапа":
            return self._tr("scan.empty_usb")
        return raw

    def _apply_idle_progress_strings(self) -> None:
        """Выставляет тексты прогресса в состоянии покоя (без активной операции)."""
        if self.is_busy:
            return
        self.progress_text.set(self._tr("progress_zero"))
        self.time_text.set(self._tr("time_initial"))
        self.progress_detail_text.set(self._tr("file_dash"))
        self.progress_speed_text.set(self._tr("speed_zero"))

    def _on_language_change(self, choice: str) -> None:
        """Обработчик смены языка в сегментированной кнопке."""
        if choice not in LanguageManager.SUPPORTED:
            return
        self.language_manager.set_language(str(choice))
        self.update_ui_language()
        self._save_app_state()

    def update_ui_language(self) -> None:
        """Обновляет все статические подписи окна под текущий язык."""
        self.root.title(self._tr("window_title", version=self.version))
        if hasattr(self, "header_title_lbl"):
            self.header_title_lbl.configure(text=self._tr("app_title"))
        if hasattr(self, "header_subtitle_lbl"):
            self.header_subtitle_lbl.configure(text=self._tr("subtitle_tagline"))
        if hasattr(self, "lang_label"):
            self.lang_label.configure(text=self._tr("language_label"))
        if hasattr(self, "feedback_prefix_label"):
            self.feedback_prefix_label.configure(text=self._tr("feedback_prefix"))
        if hasattr(self, "section_source_lbl"):
            self.section_source_lbl.configure(text=self._tr("section_source"))
        if hasattr(self, "source_hint_lbl"):
            self.source_hint_lbl.configure(text=self._tr("source_hint"))
        if hasattr(self, "refresh_button"):
            self.refresh_button.configure(text=self._tr("refresh"))
        if hasattr(self, "section_settings_lbl"):
            self.section_settings_lbl.configure(text=self._tr("section_settings"))
        if hasattr(self, "backup_hint_lbl"):
            self.backup_hint_lbl.configure(text=self._tr("backup_folder_hint"))
        if hasattr(self, "backup_target_entry"):
            self.backup_target_entry.configure(placeholder_text=self._tr("placeholder_backup_folder"))
        if hasattr(self, "browse_backup_button"):
            self.browse_backup_button.configure(text=self._tr("browse"))
        if hasattr(self, "analyze_button"):
            self.analyze_button.configure(text=self._tr("scan_folder"))
        if hasattr(self, "section_mode_lbl"):
            self.section_mode_lbl.configure(text=self._tr("section_mode"))
        if hasattr(self, "mode_hint_lbl"):
            self.mode_hint_lbl.configure(text=self._tr("mode_auto_hint"))
        if hasattr(self, "full_backup_button"):
            self.full_backup_button.configure(text=self._tr("full_backup"))
        if hasattr(self, "incremental_button"):
            self.incremental_button.configure(text=self._tr("update_backup"))
        if hasattr(self, "sync_to_usb_button"):
            self.sync_to_usb_button.configure(text=self._tr("upload_diff"))
        if hasattr(self, "autolaunch_section_lbl"):
            self.autolaunch_section_lbl.configure(text=self._tr("section_autolaunch"))
        if hasattr(self, "enable_autolaunch_button"):
            self.enable_autolaunch_button.configure(text=self._tr("autolaunch_enable"))
        if hasattr(self, "disable_autolaunch_button"):
            self.disable_autolaunch_button.configure(text=self._tr("autolaunch_disable"))
        if hasattr(self, "journal_title_lbl"):
            self.journal_title_lbl.configure(text=self._tr("section_journal"))
        if hasattr(self, "copyright_label"):
            self.copyright_label.configure(text=self._tr("copyright"))
        if hasattr(self, "language_segment"):
            self.language_segment.set(self.language_manager.language)
            self.language_segment.configure(state="disabled" if self.is_busy else "normal")
        if not self.is_busy and hasattr(self, "main_cancel_button"):
            self.main_cancel_button.configure(text=self._tr("cancel"))
        self._refresh_autolaunch_status_from_system()
        self._apply_idle_progress_strings()
        self._on_inputs_changed(clear_analysis=False)

    def _refresh_autolaunch_status_from_system(self) -> None:
        """Синхронизирует статус автозапуска с реальной задачей Планировщика."""
        try:
            target = (AutoLaunchService.get_enabled_target_serial_hex() or "").strip().upper()
            if not target:
                self.autolaunch_status_var.set(self._tr("autolaunch_status_off"))
                return
            for d in getattr(self, "devices", []) or []:
                if (d.device_id or "").strip().upper() == target:
                    self.autolaunch_status_var.set(
                        self._tr("autolaunch_status_on_drive", drive=d.drive, label=d.volume_label)
                    )
                    return
            self.autolaunch_status_var.set(self._tr("autolaunch_status_on_id", id=target))
        except Exception:
            # If anything goes wrong, keep UI usable and avoid noisy popups.
            return

    def _build_ui(self) -> None:
        available_fonts = set(tkfont.families())
        font_family = "Inter" if "Inter" in available_fonts else ("Segoe UI" if "Segoe UI" in available_fonts else "Arial")

        root_wrap = ctk.CTkFrame(self.root, fg_color=self.BG_COLOR, corner_radius=0)
        root_wrap.pack(fill="both", expand=True, padx=14, pady=12)

        header = ctk.CTkFrame(
            root_wrap,
            fg_color=self.PANEL_COLOR,
            corner_radius=10,
            border_width=1,
            border_color=self.BORDER_COLOR,
        )
        header.pack(fill="x", pady=(0, 10))
        title_row = ctk.CTkFrame(header, fg_color="transparent")
        title_row.pack(fill="x", padx=14, pady=(12, 10))
        self.header_title_lbl = ctk.CTkLabel(
            title_row,
            text=self._tr("app_title"),
            text_color=self.TEXT_COLOR,
            font=ctk.CTkFont(family=font_family, size=20, weight="bold"),
        )
        self.header_title_lbl.pack(side="left", anchor="w")
        right_block = ctk.CTkFrame(title_row, fg_color="transparent")
        right_block.pack(side="right", anchor="e")
        lang_row = ctk.CTkFrame(right_block, fg_color="transparent")
        lang_row.pack(anchor="e", pady=(0, 2))
        self.lang_label = ctk.CTkLabel(
            lang_row,
            text=self._tr("language_label"),
            text_color=self.MUTED_TEXT_COLOR,
            font=ctk.CTkFont(family=font_family, size=12),
        )
        self.lang_label.pack(side="left")
        self.language_segment = ctk.CTkSegmentedButton(
            lang_row,
            values=["ru", "en"],
            command=self._on_language_change,
            font=ctk.CTkFont(family=font_family, size=12),
            height=28,
            corner_radius=8,
            selected_color=self.ACCENT_COLOR,
            selected_hover_color=self.ACCENT_HOVER_COLOR,
            unselected_color=self.BORDER_COLOR,
            unselected_hover_color="#3a3a3a",
            text_color=self.TEXT_COLOR,
        )
        self.language_segment.set(self.language_manager.language)
        self.language_segment.pack(side="left", padx=(8, 0))
        self.header_subtitle_lbl = ctk.CTkLabel(
            right_block,
            text=self._tr("subtitle_tagline"),
            text_color=self.MUTED_TEXT_COLOR,
            font=ctk.CTkFont(family=font_family, size=13),
        )
        self.header_subtitle_lbl.pack(anchor="e")
        feedback_row = ctk.CTkFrame(right_block, fg_color="transparent")
        feedback_row.pack(anchor="e", pady=(2, 0))
        self.feedback_prefix_label = ctk.CTkLabel(
            feedback_row,
            text=self._tr("feedback_prefix"),
            text_color=self.MUTED_TEXT_COLOR,
            font=ctk.CTkFont(family=font_family, size=12),
        )
        self.feedback_prefix_label.pack(side="left")
        self.feedback_link = ctk.CTkLabel(
            feedback_row,
            text=self.FEEDBACK_EMAIL,
            text_color=self.ACCENT_COLOR,
            cursor="hand2",
            font=ctk.CTkFont(family=font_family, size=12, weight="bold"),
        )
        self.feedback_link.pack(side="left", padx=(6, 0))
        self.feedback_link.bind("<Button-1>", lambda _e: self._open_feedback_email())

        content = ctk.CTkFrame(root_wrap, fg_color="transparent")
        content.pack(fill="both", expand=True)
        content.grid_columnconfigure(0, weight=3, minsize=440)
        content.grid_columnconfigure(1, weight=2, minsize=300)
        content.grid_rowconfigure(0, weight=1)

        left_panel = ctk.CTkScrollableFrame(
            content,
            fg_color=self.PANEL_COLOR,
            corner_radius=10,
            border_width=1,
            border_color=self.BORDER_COLOR,
            scrollbar_button_color=self.BORDER_COLOR,
            scrollbar_button_hover_color="#3a3a3a",
        )
        self.left_panel = left_panel
        left_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        left_panel.grid_columnconfigure(0, weight=1)

        source_block = ctk.CTkFrame(left_panel, fg_color=self.PANEL_ALT_COLOR, corner_radius=10)
        source_block.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 8))
        source_block.grid_columnconfigure(0, weight=1)
        self.section_source_lbl = ctk.CTkLabel(
            source_block,
            text=self._tr("section_source"),
            text_color=self.TEXT_COLOR,
            font=ctk.CTkFont(family=font_family, size=14, weight="bold"),
        )
        self.section_source_lbl.grid(row=0, column=0, sticky="w", padx=12, pady=(10, 4))
        self.source_hint_lbl = ctk.CTkLabel(
            source_block,
            text=self._tr("source_hint"),
            text_color=self.MUTED_TEXT_COLOR,
            font=ctk.CTkFont(family=font_family, size=12),
        )
        self.source_hint_lbl.grid(row=1, column=0, sticky="w", padx=12, pady=(0, 6))
        self.device_combo = ctk.CTkComboBox(
            source_block,
            variable=self.selected_device,
            values=[],
            state="readonly",
            corner_radius=10,
            height=34,
            fg_color=self.BG_COLOR,
            border_color=self.BORDER_COLOR,
            button_color=self.BORDER_COLOR,
            button_hover_color="#3a3a3a",
            dropdown_fg_color=self.PANEL_COLOR,
            dropdown_hover_color="#333333",
            dropdown_text_color=self.TEXT_COLOR,
            text_color=self.TEXT_COLOR,
            font=ctk.CTkFont(family=font_family, size=12),
        )
        self.device_combo.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 8))
        self.refresh_button = ctk.CTkButton(
            source_block,
            text=self._tr("refresh"),
            command=self.refresh_devices,
            corner_radius=10,
            height=34,
            fg_color=self.BORDER_COLOR,
            hover_color="#3a3a3a",
            text_color=self.TEXT_COLOR,
            font=ctk.CTkFont(family=font_family, size=12),
        )
        self.refresh_button.grid(row=3, column=0, sticky="ew", padx=12, pady=(0, 12))

        settings_block = ctk.CTkFrame(left_panel, fg_color=self.PANEL_ALT_COLOR, corner_radius=10)
        settings_block.grid(row=1, column=0, sticky="ew", padx=10, pady=8)
        settings_block.grid_columnconfigure(0, weight=1)
        self.section_settings_lbl = ctk.CTkLabel(
            settings_block,
            text=self._tr("section_settings"),
            text_color=self.TEXT_COLOR,
            font=ctk.CTkFont(family=font_family, size=14, weight="bold"),
        )
        self.section_settings_lbl.grid(row=0, column=0, columnspan=2, sticky="w", padx=12, pady=(10, 4))
        self.backup_hint_lbl = ctk.CTkLabel(
            settings_block,
            text=self._tr("backup_folder_hint"),
            text_color=self.MUTED_TEXT_COLOR,
            font=ctk.CTkFont(family=font_family, size=12),
        )
        self.backup_hint_lbl.grid(row=1, column=0, columnspan=2, sticky="w", padx=12, pady=(0, 6))
        self.backup_target_entry = ctk.CTkEntry(
            settings_block,
            textvariable=self.backup_target_dir,
            corner_radius=10,
            height=34,
            fg_color=self.BG_COLOR,
            border_color=self.BORDER_COLOR,
            text_color=self.TEXT_COLOR,
            placeholder_text=self._tr("placeholder_backup_folder"),
            font=ctk.CTkFont(family=font_family, size=12),
        )
        self.backup_target_entry.grid(row=2, column=0, sticky="ew", padx=(12, 6), pady=(0, 8))
        self.browse_backup_button = ctk.CTkButton(
            settings_block,
            text=self._tr("browse"),
            command=self.pick_backup_target,
            width=88,
            corner_radius=10,
            height=34,
            fg_color=self.BORDER_COLOR,
            hover_color="#3a3a3a",
            text_color=self.TEXT_COLOR,
            font=ctk.CTkFont(family=font_family, size=12),
        )
        self.browse_backup_button.grid(row=2, column=1, sticky="e", padx=(0, 12), pady=(0, 8))
        self.analyze_button = ctk.CTkButton(
            settings_block,
            text=self._tr("scan_folder"),
            command=self.analyze_backup_mode,
            corner_radius=10,
            height=36,
            fg_color=self.ACCENT_COLOR,
            hover_color=self.ACCENT_HOVER_COLOR,
            text_color=self.TEXT_COLOR,
            font=ctk.CTkFont(family=font_family, size=12, weight="bold"),
        )
        self.analyze_button.grid(row=3, column=0, columnspan=2, sticky="ew", padx=12, pady=(0, 12))

        mode_block = ctk.CTkFrame(left_panel, fg_color=self.PANEL_ALT_COLOR, corner_radius=10)
        mode_block.grid(row=2, column=0, sticky="ew", padx=10, pady=8)
        mode_block.grid_columnconfigure(0, weight=1)
        self.section_mode_lbl = ctk.CTkLabel(
            mode_block,
            text=self._tr("section_mode"),
            text_color=self.TEXT_COLOR,
            font=ctk.CTkFont(family=font_family, size=14, weight="bold"),
        )
        self.section_mode_lbl.grid(row=0, column=0, sticky="w", padx=12, pady=(10, 2))
        self.mode_hint_lbl = ctk.CTkLabel(
            mode_block,
            text=self._tr("mode_auto_hint"),
            text_color=self.MUTED_TEXT_COLOR,
            font=ctk.CTkFont(family=font_family, size=11),
            wraplength=380,
            justify="left",
        )
        self.mode_hint_lbl.grid(row=1, column=0, sticky="w", padx=12, pady=(0, 8))
        self.full_backup_button = ctk.CTkButton(
            mode_block,
            text=self._tr("full_backup"),
            command=self.run_full_backup,
            corner_radius=10,
            height=34,
            font=ctk.CTkFont(family=font_family, size=11, weight="bold"),
        )
        self.full_backup_button.grid(row=2, column=0, sticky="ew", padx=12, pady=4)
        self.incremental_button = ctk.CTkButton(
            mode_block,
            text=self._tr("update_backup"),
            command=self.run_incremental_backup,
            corner_radius=10,
            height=34,
            font=ctk.CTkFont(family=font_family, size=11, weight="bold"),
        )
        self.incremental_button.grid(row=3, column=0, sticky="ew", padx=12, pady=4)
        self.sync_to_usb_button = ctk.CTkButton(
            mode_block,
            text=self._tr("upload_diff"),
            command=self.run_sync_to_usb,
            corner_radius=10,
            height=34,
            font=ctk.CTkFont(family=font_family, size=11, weight="bold"),
        )
        self.sync_to_usb_button.grid(row=4, column=0, sticky="ew", padx=12, pady=(4, 12))

        autolaunch_block = ctk.CTkFrame(left_panel, fg_color=self.PANEL_ALT_COLOR, corner_radius=10)
        self.autolaunch_block = autolaunch_block
        autolaunch_block.grid(row=3, column=0, sticky="ew", padx=10, pady=(8, 10))
        autolaunch_block.grid_columnconfigure(0, weight=1)
        self.autolaunch_section_lbl = ctk.CTkLabel(
            autolaunch_block,
            text=self._tr("section_autolaunch"),
            text_color=self.TEXT_COLOR,
            font=ctk.CTkFont(family=font_family, size=14, weight="bold"),
        )
        self.autolaunch_section_lbl.grid(row=0, column=0, sticky="w", padx=12, pady=(10, 4))
        self.autolaunch_status_label = ctk.CTkLabel(
            autolaunch_block,
            textvariable=self.autolaunch_status_var,
            text_color=self.MUTED_TEXT_COLOR,
            font=ctk.CTkFont(family=font_family, size=12),
            wraplength=380,
            justify="left",
        )
        self.autolaunch_status_label.grid(row=1, column=0, sticky="w", padx=12, pady=(0, 8))
        self.enable_autolaunch_button = ctk.CTkButton(
            autolaunch_block,
            text=self._tr("autolaunch_enable"),
            command=self.enable_usb_autolaunch,
            corner_radius=10,
            height=34,
            fg_color=self.BORDER_COLOR,
            hover_color="#3a3a3a",
            text_color=self.TEXT_COLOR,
            font=ctk.CTkFont(family=font_family, size=12),
        )
        self.enable_autolaunch_button.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 6))
        self.disable_autolaunch_button = ctk.CTkButton(
            autolaunch_block,
            text=self._tr("autolaunch_disable"),
            command=self.disable_usb_autolaunch,
            corner_radius=10,
            height=34,
            fg_color=self.BORDER_COLOR,
            hover_color="#3a3a3a",
            text_color=self.TEXT_COLOR,
            font=ctk.CTkFont(family=font_family, size=12),
        )
        self.disable_autolaunch_button.grid(row=3, column=0, sticky="ew", padx=12, pady=(0, 12))

        right_panel = ctk.CTkFrame(
            content,
            fg_color=self.PANEL_COLOR,
            corner_radius=10,
            border_width=1,
            border_color=self.BORDER_COLOR,
        )
        right_panel.grid(row=0, column=1, sticky="nsew")
        right_panel.grid_columnconfigure(0, weight=1)
        right_panel.grid_rowconfigure(1, weight=1)
        self.journal_title_lbl = ctk.CTkLabel(
            right_panel,
            text=self._tr("section_journal"),
            text_color=self.TEXT_COLOR,
            font=ctk.CTkFont(family=font_family, size=16, weight="bold"),
        )
        self.journal_title_lbl.grid(row=0, column=0, sticky="w", padx=14, pady=(12, 0))
        self.log_wrap = ctk.CTkFrame(right_panel, fg_color=self.PANEL_ALT_COLOR, corner_radius=10)
        self.log_wrap.grid(row=1, column=0, sticky="nsew", padx=12, pady=(10, 12))
        self.log_wrap.grid_columnconfigure(0, weight=1)
        self.log_wrap.grid_rowconfigure(0, weight=1)
        self.log_box = ctk.CTkTextbox(
            self.log_wrap,
            corner_radius=10,
            fg_color=self.BG_COLOR,
            border_width=1,
            border_color=self.BORDER_COLOR,
            text_color=self.TEXT_COLOR,
            wrap="word",
            activate_scrollbars=True,
            font=ctk.CTkFont(family=font_family, size=12),
        )
        self.log_box.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)
        self.log_box.bind("<MouseWheel>", self._on_log_mousewheel)
        self.log_box.bind("<Button-4>", self._on_log_mousewheel)
        self.log_box.bind("<Button-5>", self._on_log_mousewheel)
        self.log_box.configure(state="disabled")
        self.log_box._textbox.tag_config("system", foreground=self.LOG_SYSTEM_COLOR)
        self.log_box._textbox.tag_config("success", foreground=self.LOG_SUCCESS_COLOR)
        self.log_box._textbox.tag_config("error", foreground=self.LOG_ERROR_COLOR)

        self.status_line = ctk.CTkLabel(
            root_wrap,
            textvariable=self.status,
            text_color=self.MUTED_TEXT_COLOR,
            anchor="w",
            font=ctk.CTkFont(family=font_family, size=12),
        )
        self.status_line.pack(fill="x", padx=2, pady=(8, 2))
        self.progress_frame = ctk.CTkFrame(
            root_wrap,
            fg_color=self.PANEL_COLOR,
            corner_radius=10,
            border_width=1,
            border_color=self.BORDER_COLOR,
        )
        self.progress_frame.pack(fill="x")
        self.progress_bar = ctk.CTkProgressBar(
            self.progress_frame,
            corner_radius=10,
            height=16,
            progress_color=self.ACCENT_COLOR,
            fg_color=self.BORDER_COLOR,
        )
        self.progress_bar.pack(fill="x", padx=10, pady=(10, 8))
        self.progress_bar.set(0)
        ctk.CTkLabel(
            self.progress_frame,
            textvariable=self.progress_text,
            text_color=self.TEXT_COLOR,
            anchor="w",
            font=ctk.CTkFont(family=font_family, size=12),
        ).pack(fill="x", padx=10)
        ctk.CTkLabel(
            self.progress_frame,
            textvariable=self.progress_detail_text,
            text_color=self.TEXT_COLOR,
            anchor="w",
            font=ctk.CTkFont(family=font_family, size=12),
        ).pack(fill="x", padx=10, pady=(2, 0))
        ctk.CTkLabel(
            self.progress_frame,
            textvariable=self.time_text,
            text_color=self.MUTED_TEXT_COLOR,
            anchor="w",
            font=ctk.CTkFont(family=font_family, size=12),
        ).pack(fill="x", padx=10, pady=(2, 0))
        ctk.CTkLabel(
            self.progress_frame,
            textvariable=self.progress_speed_text,
            text_color=self.MUTED_TEXT_COLOR,
            anchor="w",
            font=ctk.CTkFont(family=font_family, size=12),
        ).pack(fill="x", padx=10, pady=(2, 8))
        self.main_cancel_button = ctk.CTkButton(
            self.progress_frame,
            text=self._tr("cancel"),
            command=self._cancel_active_operation,
            corner_radius=10,
            height=32,
            width=150,
            fg_color=self.BORDER_COLOR,
            hover_color="#3a3a3a",
            text_color=self.TEXT_COLOR,
            state="disabled",
            font=ctk.CTkFont(family=font_family, size=12, weight="bold"),
        )
        self.main_cancel_button.pack(anchor="e", padx=10, pady=(0, 10))
        self._set_progress_visible(False)

        self.copyright_label = ctk.CTkLabel(
            root_wrap,
            text=self._tr("copyright"),
            text_color=self.MUTED_TEXT_COLOR,
            font=ctk.CTkFont(family=font_family, size=10),
        )
        self.copyright_label.pack(fill="x", pady=(8, 0))

    def _open_feedback_email(self) -> None:
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(self.FEEDBACK_EMAIL)
            self.root.update_idletasks()
            messagebox.showinfo(
                self._tr("msg_feedback_title"),
                self._tr("msg_feedback_copied", email=self.FEEDBACK_EMAIL),
            )
        except Exception:
            messagebox.showinfo(
                self._tr("msg_feedback_title"),
                self._tr("msg_feedback_plain", email=self.FEEDBACK_EMAIL),
            )

    def _available_actions_status(self, allow_full: bool, allow_to_backup: bool, allow_to_usb: bool) -> str:
        actions = []
        if allow_full:
            actions.append(self._tr("full_backup"))
        if allow_to_backup:
            actions.append(self._tr("update_backup"))
        if allow_to_usb:
            actions.append(self._tr("upload_diff"))
        if not actions:
            return self._tr("no_actions")
        return self._tr("actions_available", list=", ".join(actions))

    def _log(self, text: str, important: bool = True) -> None:
        if self.laconic_log and not important:
            return
        lowered = text.lower()
        if any(
            token in lowered
            for token in (
                "ошибка",
                "не удалось",
                "отменено",
                "exception",
                "fail",
                "error",
                "cancelled",
                "canceled",
            )
        ):
            tag_name = "error"
        elif any(
            token in lowered
            for token in (
                "готово",
                "заверш",
                "включен",
                "успеш",
                "совпадает",
                "done",
                "complete",
                "enabled",
                "matches",
            )
        ):
            tag_name = "success"
        else:
            tag_name = "system"
        self.log_box.configure(state="normal")
        self.log_box._textbox.insert(END, f"{datetime.now().strftime('%H:%M:%S')} | {text}\n", (tag_name,))
        if self.log_autofollow:
            self.log_box.see(END)
        self.log_box.configure(state="disabled")
        self._update_log_autofollow_from_view()

    def _is_log_at_bottom(self) -> bool:
        if not hasattr(self, "log_box"):
            return True
        first, last = self.log_box.yview()
        # Before layout settles Tk can report 0..0, treat as follow mode.
        if first == 0.0 and last == 0.0:
            return True
        return last >= 0.995

    def _update_log_autofollow_from_view(self) -> None:
        self.log_autofollow = self._is_log_at_bottom()

    def _on_log_yscroll(self, first, last) -> None:
        if hasattr(self, "log_scroll"):
            self.log_scroll.set(first, last)
        self._update_log_autofollow_from_view()

    def _on_log_scrollbar(self, *args) -> None:
        self.log_box.yview(*args)
        self._update_log_autofollow_from_view()

    def _on_log_mousewheel(self, event) -> str:
        # Windows/macOS mouse wheel
        if hasattr(event, "delta") and event.delta:
            step = int(-event.delta / 120)
            if step == 0:
                step = -1 if event.delta > 0 else 1
            self.log_box.yview_scroll(step, "units")
            self._update_log_autofollow_from_view()
            return "break"
        # Linux wheel events
        if getattr(event, "num", None) == 4:
            self.log_box.yview_scroll(-1, "units")
            self._update_log_autofollow_from_view()
            return "break"
        if getattr(event, "num", None) == 5:
            self.log_box.yview_scroll(1, "units")
            self._update_log_autofollow_from_view()
            return "break"
        return ""

    @staticmethod
    def _fmt_seconds(seconds) -> str:
        return ProgressPresenter.format_seconds(seconds)

    def _fmt_size(self, num_bytes: int) -> str:
        """Форматирует размер с учётом текущего языка."""
        return ProgressPresenter.format_size(num_bytes, self._size_units())

    def _copy_file_streaming(
        self,
        src: Path,
        dst: Path,
        rel: str,
        bytes_total: int,
        start_ts: float,
        action: str,
        done_files: int,
        total_files: int,
        bytes_done_before: int,
    ) -> int:
        def _on_chunk_progress(copied: int) -> None:
            total_bytes_done = bytes_done_before + copied
            self.root.after(
                0,
                lambda bd=total_bytes_done, r=rel, s=start_ts: self._update_progress_ui(
                    done_files,
                    total_files,
                    bd,
                    bytes_total,
                    r,
                    s,
                    action,
                ),
            )

        return BackupLogic.copy_file_streaming(
            src,
            dst,
            on_chunk_progress=_on_chunk_progress,
            cancel_event=self.transfer_cancel_event,
        )

    def _scan_files_progress(self, root: Path, on_progress: callable = None, cancel_event: threading.Event = None) -> dict:
        return BackupLogic.scan_files_progress(root, on_progress=on_progress, cancel_event=cancel_event)

    def _file_hash(self, path: Path, cancel_event: threading.Event = None) -> str:
        return BackupLogic.file_hash(path, cancel_event=cancel_event)

    def _selected_device_info(self):
        selected = self.selected_device.get()
        chosen_drive = ""
        try:
            # expected like "D:\ (NAME)"
            if len(selected) >= 3 and selected[1] == ":":
                chosen_drive = selected[:3].upper()
        except Exception:
            chosen_drive = ""
        for d in self.devices:
            if chosen_drive and d.drive.upper() == chosen_drive:
                return d
        return None

    def _set_action_button_state(self, button, enabled: bool) -> None:
        if enabled:
            button.configure(
                state="normal",
                fg_color=self.ACCENT_COLOR,
                hover_color=self.ACCENT_HOVER_COLOR,
                text_color=self.TEXT_COLOR,
            )
        else:
            button.configure(
                state="disabled",
                fg_color=self.DISABLED_COLOR,
                hover_color=self.DISABLED_COLOR,
                text_color=self.MUTED_TEXT_COLOR,
            )

    def _update_action_buttons(self, mode: str, can_sync_to_backup: bool = False, can_sync_to_usb: bool = False) -> None:
        if self.is_busy:
            return
        self._set_action_button_state(self.full_backup_button, mode == "full")
        self._set_action_button_state(self.incremental_button, mode == "sync" and can_sync_to_backup)
        self._set_action_button_state(self.sync_to_usb_button, mode == "sync" and can_sync_to_usb)

    def refresh_devices(self) -> None:
        self.devices = DeviceDetector.list_source_devices()
        labels = [f"{d.drive} ({d.volume_label})" for d in self.devices]
        self.device_combo.configure(values=labels)
        # Do not auto-select the first device: users may have multiple USB drives connected.
        # Keep the existing selection if it is still valid; otherwise clear and require explicit choice.
        current = (self.selected_device.get() or "").strip()
        if current and current not in labels:
            self.selected_device.set("")
        self.status.set(self._tr("sources_found", n=len(self.devices)))
        self._log(self._tr("log_devices_found", n=len(labels)), important=False)
        self._on_inputs_changed()

    def _maybe_autostart_scan(self) -> None:
        if self.is_busy:
            return
        if not self.autostart_device_id:
            return
        if not self.backup_target_dir.get().strip():
            self._log(self._tr("log_autostart_skipped"))
            return

        wanted = _normalize_autostart_device_id(self.autostart_device_id)
        for d in self.devices:
            if d.device_id == wanted:
                label = f"{d.drive} ({d.volume_label})"
                self._log(self._tr("log_autostart_found", label=label))
                self.autolaunch_status_var.set(
                    self._tr("autolaunch_status_on_drive", drive=d.drive, label=d.volume_label)
                )
                self.selected_device.set(label)
                self.analyze_backup_mode()
                return

        # Retry while Windows finishes mounting USB after event trigger.
        if self._autostart_retry_left > 0:
            self._autostart_retry_left -= 1
            self.refresh_devices()
            self.root.after(1500, self._maybe_autostart_scan)
            return

        self._log(self._tr("log_autostart_not_found"), important=False)

    def _on_inputs_changed(self, *, clear_analysis: bool = True) -> None:
        if self.is_busy:
            return
        if clear_analysis:
            self.analysis = None
            self._update_action_buttons("none", False, False)
        if self.selected_device.get().strip() and self.backup_target_dir.get().strip():
            self.status.set(self._tr("hint_scan_for_diff"))

    def pick_backup_target(self) -> None:
        folder = filedialog.askdirectory(title=self._tr("pick_backup_folder_dialog"))
        if folder:
            self.backup_target_dir.set(folder)

    @staticmethod
    def _is_admin() -> bool:
        return AutoLaunchService.is_admin()

    def enable_usb_autolaunch(self) -> None:
        if not self._is_admin():
            messagebox.showerror(self._tr("msg_admin_title"), self._tr("msg_admin_enable_body"))
            return
        device = self._selected_device_info()
        if not device:
            messagebox.showerror(self._tr("msg_usb_title"), self._tr("msg_usb_pick"))
            return

        try:
            result = AutoLaunchService.create_task_for_device(
                drive_letter=device.drive,
                autostart_device_id=device.device_id,
                entry_script_path=Path(__file__).resolve(),
            )
        except Exception as e:
            messagebox.showerror(
                self._tr("msg_error_title"),
                self._tr("msg_instance_path_body", reason=str(e)),
            )
            return

        try:
            if int(result["returncode"]) != 0:
                err_text = str(result["stderr"] or result["stdout"] or "").strip() or f"schtasks failed: {result['returncode']}"
                raise RuntimeError(err_text)
            self.autolaunch_status_var.set(self._tr("autolaunch_status_on_label", label=device.volume_label))
            self._log(self._tr("log_autostart_enabled", label=device.volume_label))
            messagebox.showinfo(self._tr("msg_done_title"), self._tr("msg_autolaunch_on_body"))
        except Exception as e:
            messagebox.showerror(
                self._tr("msg_error_title"),
                self._tr(
                    "msg_autolaunch_task_fail",
                    error=str(e),
                    instance_path=str(result.get("instance_path", "-")),
                ),
            )

    def disable_usb_autolaunch(self) -> None:
        if not self._is_admin():
            messagebox.showerror(self._tr("msg_admin_title"), self._tr("msg_admin_disable_body"))
            return
        try:
            result = AutoLaunchService.delete_task()
            # If task does not exist, schtasks returns errorlevel 1; treat as ok.
            if int(result["returncode"]) not in (0, 1):
                err_text = str(result["stderr"] or result["stdout"] or "").strip() or f"schtasks failed: {result['returncode']}"
                raise RuntimeError(err_text)
            self.autolaunch_status_var.set(self._tr("autolaunch_status_off"))
            self._log(self._tr("log_autostart_disabled"))
            messagebox.showinfo(self._tr("msg_done_title"), self._tr("msg_autolaunch_off_body"))
        except Exception as e:
            messagebox.showerror(self._tr("msg_error_title"), self._tr("msg_autolaunch_disable_fail", error=str(e)))

    def _save_app_state(self) -> None:
        """Сохраняет путь бэкапа и язык, объединяя с существующим JSON."""
        merged: dict = {}
        if APP_STATE_PATH.exists():
            try:
                merged = json.loads(APP_STATE_PATH.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                merged = {}
        merged["last_backup_path"] = self.backup_target_dir.get().strip()
        merged["language"] = self.language_manager.language
        try:
            APP_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            APP_STATE_PATH.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    def analyze_backup_mode(self) -> None:
        """Запускает асинхронное сканирование с прогресс-баром и возможностью отмены."""
        if self.is_busy:
            return

        device = self._selected_device_info()
        target_text = self.backup_target_dir.get().strip()
        if not device or not target_text:
            self.analysis = None
            self._update_action_buttons("none")
            return
        source = Path(device.drive)
        target = Path(target_text)
        target.mkdir(parents=True, exist_ok=True)

        self._set_busy(True, self._tr("busy_scanning"))
        self._open_scan_window()
        self.scan_cancel_event = threading.Event()

        start_ts = time.monotonic()

        def scan_worker():
            try:
                def on_scan_progress(scanned: int, total: int, current: str):
                    self.root.after(0, lambda: self._update_scan_ui(
                        scanned, total, current, start_ts, total == 0
                    ))

                self._log(self._tr("log_scan_started"))
                last_analyze_update = 0.0

                def on_compare_progress(processed: int, total: int, rel: str) -> None:
                    nonlocal last_analyze_update
                    if self.scan_cancel_event and self.scan_cancel_event.is_set():
                        return
                    now_a = time.monotonic()
                    if processed == 1 or now_a - last_analyze_update > 0.2:
                        self.root.after(
                            0,
                            lambda p=processed, t=total, c=rel: self._update_scan_ui(
                                p,
                                t,
                                self._tr("analyze_line", path=c),
                                start_ts,
                                False,
                            ),
                        )
                        last_analyze_update = now_a

                try:
                    result, meta = ScanService.analyze(
                        source=source,
                        target=target,
                        scan_files_fn=self._scan_files_progress,
                        file_hash_fn=self._file_hash,
                        cancel_event=self.scan_cancel_event,
                        on_scan_progress=on_scan_progress,
                        on_compare_progress=on_compare_progress,
                    )
                except RuntimeError:
                    self.root.after(0, lambda: self._finish_scan(None, True))
                    return

                mode = result.get("mode", "none")
                copy_to_backup_ops = result.get("ops_to_backup", [])
                copy_to_usb_ops = result.get("ops_to_usb", [])
                matched = int(meta.get("matched", 0))
                src_count = int(meta.get("src_count", 0))
                dst_count = int(meta.get("dst_count", 0))
                pre_status_msg = str(meta.get("status_msg", "")).strip()

                if pre_status_msg:
                    localized_pre = self._localize_scan_service_message(pre_status_msg)
                    self.root.after(0, lambda s=localized_pre: self._finish_scan(result, False, s))
                    return

                if mode == "full":
                    status_msg = self._available_actions_status(True, False, False)
                    self._log(self._tr("log_mode_full", src=src_count, dst=dst_count))
                elif mode == "sync":
                    status_msg = self._available_actions_status(False, bool(copy_to_backup_ops), bool(copy_to_usb_ops))
                    self._log(
                        self._tr(
                            "log_mode_diff",
                            to_backup=len(copy_to_backup_ops),
                            to_usb=len(copy_to_usb_ops),
                        )
                    )
                else:
                    status_msg = self._tr("status_folder_up_to_date")
                    self._log(self._tr("log_backup_not_needed", matched=matched))
                self.root.after(0, lambda: self._finish_scan(result, False, status_msg))

            except Exception as e:
                err_s = str(e)
                self.root.after(0, lambda err=err_s: self._finish_scan(None, True, self._tr("log_scan_error", error=err)))
            finally:
                if self.scan_cancel_event and self.scan_cancel_event.is_set():
                    self.root.after(0, lambda: self._finish_scan(None, True))

        threading.Thread(target=scan_worker, daemon=True).start()

    def _set_busy(self, busy: bool, text: str) -> None:
        self.is_busy = busy
        state = "disabled" if busy else "normal"
        self.device_combo.configure(state="disabled" if busy else "readonly")
        self.refresh_button.configure(state=state)
        self.browse_backup_button.configure(state=state)
        self.analyze_button.configure(state=state)
        self.enable_autolaunch_button.configure(state=state)
        self.disable_autolaunch_button.configure(state=state)
        if hasattr(self, "language_segment"):
            self.language_segment.configure(state="disabled" if busy else "normal")
        self._set_progress_visible(busy)
        if busy:
            self._set_action_button_state(self.full_backup_button, False)
            self._set_action_button_state(self.incremental_button, False)
            self._set_action_button_state(self.sync_to_usb_button, False)
            self.main_cancel_button.configure(state="normal")
        else:
            self.progress_bar.set(0)
            self.progress_text.set(self._tr("progress_zero"))
            self.time_text.set(self._tr("time_initial"))
            self.progress_detail_text.set(self._tr("file_dash"))
            self.progress_speed_text.set(self._tr("speed_zero"))
            self.main_cancel_button.configure(state="disabled", text=self._tr("cancel"))
            if self.analysis:
                self._update_action_buttons(
                    self.analysis.get("mode", "none"),
                    bool(self.analysis.get("ops_to_backup")),
                    bool(self.analysis.get("ops_to_usb")),
                )
            else:
                self._update_action_buttons("none")
        self.status.set(text)

    def _set_progress_visible(self, visible: bool) -> None:
        if not hasattr(self, "progress_frame"):
            return
        if visible:
            if self.progress_frame.winfo_manager() != "pack":
                self.progress_frame.pack(fill="x")
        else:
            if self.progress_frame.winfo_manager() == "pack":
                self.progress_frame.pack_forget()

    def _open_scan_window(self) -> None:
        self.active_operation = "scan"
        self.main_cancel_button.configure(text=self._tr("stop_scan"), state="normal")
        self.progress_text.set(self._tr("scanning_zero"))
        self.progress_detail_text.set(self._tr("file_prep"))
        self.progress_speed_text.set(self._tr("speed_na"))

    def _close_scan_window(self) -> None:
        self.active_operation = None
        self.main_cancel_button.configure(state="disabled", text=self._tr("cancel"))

    def _update_scan_ui(self, scanned: int, total: int, current: str, start_ts: float, is_indeterminate: bool = False) -> None:
        now = time.monotonic()
        if now - self.last_progress_update < 0.08:
            return
        self.last_progress_update = now

        elapsed = max(0.001, now - start_ts)
        short_current = current if len(current) < 78 else current[:75] + "..."
        percent = int((scanned / max(1, total)) * 100) if (total > 0 and not is_indeterminate) else 0
        self.progress_bar.set(percent / 100.0 if total > 0 and not is_indeterminate else 0.0)
        self.progress_text.set(
            self._tr("scan_progress_pct", pct=percent, done=scanned, total=max(1, total))
            if total > 0
            else self._tr("scan_progress_only", done=scanned)
        )
        self.progress_detail_text.set(self._tr("file_current", name=short_current))
        self.time_text.set(
            self._tr(
                "elapsed_remaining",
                elapsed=self._fmt_seconds(elapsed),
                remaining="--:--:--",
            )
        )
        self.progress_speed_text.set(self._tr("speed_na"))

    def _cancel_scan(self) -> None:
        if self.scan_cancel_event:
            self.scan_cancel_event.set()
        self.main_cancel_button.configure(state="disabled")
        self._log(self._tr("log_scan_cancelled"))
        self._close_scan_window()
        self._set_busy(False, self._tr("scan_cancelled_status"))
        self.status.set(self._tr("scan_cancelled_hint"))

    def _close_transfer_window(self) -> None:
        self.transfer_cancel_requested = False
        self.transfer_cancel_event = None
        self.active_operation = None
        self.main_cancel_button.configure(state="disabled", text=self._tr("cancel"))

    def _cancel_transfer(self) -> None:
        if self.transfer_cancel_requested:
            return
        self.transfer_cancel_requested = True
        self.main_cancel_button.configure(state="disabled")
        if self.transfer_cancel_event:
            self.transfer_cancel_event.set()
        self._log(self._tr("log_transfer_cancelled"))
        self.status.set(self._tr("transfer_cancelling"))

    def _open_transfer_window(self, action: str) -> None:
        self.transfer_cancel_requested = False
        self.transfer_cancel_event = threading.Event()
        self.active_operation = "copy"
        self.main_cancel_button.configure(text=self._tr("stop_copy"), state="normal")
        self.progress_text.set(self._tr("copy_progress", action=action, pct=0, done=0, total=0))
        self.progress_detail_text.set(self._tr("file_prep"))
        self.progress_speed_text.set(self._tr("speed_zero"))

    def _update_progress_ui(self, done: int, total: int, bytes_done: int, bytes_total: int, current: str, start_ts: float, action: str) -> None:
        short_current = current if len(current) < 78 else current[:75] + "..."
        if bytes_total > 0:
            percent = int((bytes_done / max(1, bytes_total)) * 100)
        else:
            percent = int((done / max(1, total)) * 100)
        elapsed = max(0.001, time.monotonic() - start_ts)
        speed_bps = bytes_done / elapsed
        remaining_bytes = max(0, bytes_total - bytes_done)
        eta = (remaining_bytes / speed_bps) if speed_bps > 0 else None

        self.progress_bar.set(percent / 100.0)
        self.progress_text.set(self._tr("copy_progress", action=action, pct=percent, done=done, total=total))
        self.progress_detail_text.set(self._tr("file_current", name=short_current))
        self.time_text.set(
            self._tr(
                "elapsed_remaining",
                elapsed=self._fmt_seconds(elapsed),
                remaining=self._fmt_seconds(eta),
            )
        )
        self.progress_speed_text.set(
            ProgressPresenter.format_speed_and_remaining(
                speed_bps,
                total - done,
                remaining_bytes,
                units=self._size_units(),
                speed_template=self._tr("progress.speed_mbps"),
                items_template=self._tr("progress.items_remaining"),
            )
        )

    def _cancel_active_operation(self) -> None:
        if self.active_operation == "scan":
            self._cancel_scan()
            return
        if self.active_operation == "copy":
            self._cancel_transfer()
            return

    def _run_copy_job(self, direction: str) -> None:
        if direction == "to_backup":
            self._log(self._tr("log_action_to_backup", action=self._tr("update_backup")), important=False)
        elif direction == "to_usb":
            self._log(self._tr("log_action_to_usb", action=self._tr("upload_diff")), important=False)
        else:
            self._log(self._tr("log_action_copy"), important=False)
        try:
            if self.is_busy:
                messagebox.showinfo(self._tr("msg_busy_title"), self._tr("msg_busy_body"))
                return
            if not self.analysis:
                messagebox.showerror(self._tr("msg_need_scan_title"), self._tr("msg_need_scan_body"))
                return

            if direction == "to_backup":
                ops = self.analysis.get("ops_to_backup", [])
                bytes_total_value = self.analysis.get("bytes_to_backup", 0)
                action = self._tr("job_to_backup")
            elif direction == "to_usb":
                ops = self.analysis.get("ops_to_usb", [])
                bytes_total_value = self.analysis.get("bytes_to_usb", 0)
                action = self._tr("job_to_usb")
            else:
                ops = []
                bytes_total_value = 0
                action = self._tr("job_copy")

            self._log(self._tr("log_prep", action=action, n=len(ops)), important=False)
            if not ops:
                messagebox.showinfo(self._tr("msg_info_title"), self._tr("msg_nothing_to_copy"))
                return

            running = self._tr("job_running", action=action.lower())
            self._set_busy(True, running)
            self._open_transfer_window(self._tr("transfer_short"))
            self._log(
                self._tr("log_started", action=action, n=len(ops), size=self._fmt_size(bytes_total_value)),
            )
        except Exception as e:
            self._set_busy(False, self._tr("error_status"))
            self._log(self._tr("log_copy_start_fail", error=str(e)))
            messagebox.showerror(self._tr("msg_error_title"), self._tr("msg_copy_start_fail", error=str(e)))
            return

        def worker() -> None:
            def on_progress(done: int, total: int, bytes_done: int, bytes_total: int, rel: str, start_ts: float, progress_action: str) -> None:
                self.root.after(
                    0,
                    lambda d=done, t=total, bd=bytes_done, bt=bytes_total, r=rel, s=start_ts, a=progress_action: self._update_progress_ui(
                        d, t, bd, bt, r, s, a
                    ),
                )

            def on_cancelled(cancelled_action: str, done: int, bytes_done: int) -> None:
                self.root.after(0, lambda a=cancelled_action, d=done, bd=bytes_done: self._finish_job_cancelled(a, d, bd))

            def on_finished(finished_action: str, total: int, bytes_total: int, skipped: list[tuple[str, str]]) -> None:
                self.root.after(0, lambda a=finished_action, t=total, bt=bytes_total, p=skipped: self._finish_job(a, t, bt, p))

            def on_error(exc: Exception, rel: str) -> None:
                err_text = str(exc)
                err_file = rel

                def on_err() -> None:
                    self._close_transfer_window()
                    self._set_busy(False, self._tr("copy_error_status"))
                    self._log(self._tr("log_copy_file_error", file=err_file, reason=err_text))
                    messagebox.showerror(
                        self._tr("msg_copy_file_error_title"),
                        self._tr("msg_copy_file_error_body", file=err_file, reason=err_text),
                    )

                self.root.after(0, on_err)

            TransferService.run_copy_loop(
                ops=ops,
                action=action,
                bytes_total=int(bytes_total_value),
                cancel_event=self.transfer_cancel_event,
                copy_streaming_fn=self._copy_file_streaming,
                on_progress=on_progress,
                on_cancelled=on_cancelled,
                on_finished=on_finished,
                on_error=on_error,
            )

        threading.Thread(target=worker, daemon=True).start()

    def _finish_job_cancelled(self, action: str, done: int, bytes_done: int) -> None:
        self._close_transfer_window()
        self.analysis = None
        self._update_action_buttons("none")
        self._set_busy(False, self._tr("transfer_cancelled_status"))
        self._log(self._tr("log_job_cancelled", action=action, done=done, size=self._fmt_size(bytes_done)))
        self.status.set(self._tr("operation_cancelled_files", n=done))

    def _finish_scan(self, analysis_result: dict = None, was_cancelled: bool = False, status_msg: str = None) -> None:
        """Завершает сканирование, обновляет UI и analysis."""
        self._close_scan_window()

        if was_cancelled:
            self.analysis = None
            self._update_action_buttons("none")
            self._set_busy(False, self._tr("scan_cancelled_status"))
            return

        # Mark not busy BEFORE enabling action buttons.
        self._set_busy(False, self._tr("done_status"))

        if analysis_result is not None:
            self.analysis = analysis_result
            mode = analysis_result["mode"]
            self._update_action_buttons(
                mode,
                bool(analysis_result.get("ops_to_backup")),
                bool(analysis_result.get("ops_to_usb"))
            )
            if status_msg:
                self.status.set(status_msg)
                self._log(status_msg)
            else:
                self.status.set(self._tr("analyzing_done"))
        else:
            self.analysis = None
            self._update_action_buttons("none")
            self.status.set(self._tr("scan_failed_status"))

    def _finish_job(self, action: str, total: int, bytes_total: int, permission_skipped: list[tuple[str, str]] | None = None) -> None:
        skipped = permission_skipped or []
        copied = max(0, total - len(skipped))
        if skipped:
            self._log(
                self._tr(
                    "log_job_done_warn",
                    action=action,
                    copied=copied,
                    total=total,
                    skipped=len(skipped),
                    size=self._fmt_size(bytes_total),
                )
            )
        else:
            self._log(self._tr("log_job_done", action=action, total=total, size=self._fmt_size(bytes_total)))
        self._close_transfer_window()
        if skipped:
            self._set_busy(False, self._tr("operation_done_warnings"))
            details = "\n".join(f"- {p}" for p, _ in skipped[:5])
            tail = (
                ""
                if len(skipped) <= 5
                else self._tr("msg_warnings_tail", n=len(skipped) - 5)
            )
            messagebox.showwarning(
                self._tr("msg_warnings_title"),
                self._tr(
                    "msg_warnings_body",
                    skipped=len(skipped),
                    details=details,
                    tail=tail,
                ),
            )
        else:
            self._set_busy(False, self._tr("operation_done"))
        self.analysis = None
        self._update_action_buttons("none")
        self.status.set(
            self._tr("operation_done_warnings") if skipped else self._tr("operation_done")
        )

    def run_full_backup(self) -> None:
        self._run_copy_job("to_backup")

    def run_incremental_backup(self) -> None:
        self._run_copy_job("to_backup")

    def run_sync_to_usb(self) -> None:
        self._run_copy_job("to_usb")
