"""Thin application entrypoint."""

import argparse
import ctypes
import subprocess
import sys
from pathlib import Path

import customtkinter as ctk

from ui.main_window import BackupApp

from device_detector import DeviceDetector

ERROR_ALREADY_EXISTS = 183
_SINGLE_INSTANCE_MUTEX = None


def main() -> None:
    """Parse CLI args, enforce single-instance/elevation, then start UI loop."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--autostart-device-id", default="")
    parser.add_argument("--autostart-volume-label", default="")
    parser.add_argument("--elevated", action="store_true")
    args, _unknown = parser.parse_known_args()

    # If Task Scheduler triggers on unrelated device arrivals, we may still be launched
    # with the saved autostart ID. Exit early unless the target device is actually present.
    if args.autostart_device_id:
        try:
            present_ids = [d.device_id for d in DeviceDetector.list_source_devices()]
            if args.autostart_device_id not in present_ids:
                return
        except Exception as e:
            _ = e

    # Task Scheduler can emit several arrival events quickly.
    # Avoid multiple app windows for one USB insert.
    if args.autostart_device_id:
        try:
            global _SINGLE_INSTANCE_MUTEX
            _SINGLE_INSTANCE_MUTEX = ctypes.windll.kernel32.CreateMutexW(None, False, "Global\\USBBackupApp_Autostart")
            if ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
                return
        except Exception:
            pass

    # Auto-elevate to admin (UAC prompt) when needed.
    # Many operations (diskpart, task scheduler, enabling logs) require admin rights.
    if not BackupApp._is_admin() and not args.elevated:
        try:
            tail = [a for a in sys.argv[1:] if a != "--elevated"]
            if getattr(sys, "frozen", False):
                exe_path = str(Path(sys.executable).resolve())
                cwd = str(Path(sys.executable).resolve().parent)
                params = subprocess.list2cmdline(tail + ["--elevated"])
                ctypes.windll.shell32.ShellExecuteW(None, "runas", exe_path, params, cwd, 1)
            else:
                script_path = str(Path(__file__).resolve())
                cwd = str(Path(__file__).resolve().parents[1])
                params = subprocess.list2cmdline([script_path] + tail + ["--elevated"])
                ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, cwd, 1)
        except Exception:
            pass
        return

    root = ctk.CTk()
    app = BackupApp(
        root,
        autostart_device_id=args.autostart_device_id,
        autostart_volume_label=args.autostart_volume_label,
    )
    _ = app
    root.mainloop()


if __name__ == "__main__":
    main()

