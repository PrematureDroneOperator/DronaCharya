from __future__ import annotations

import tkinter as tk
from tkinter import ttk


class DroneGUI:
    def __init__(self, controller) -> None:
        self.controller = controller
        self.root = tk.Tk()
        self.root.title("dronAcharya - Onboard Control")
        self.root.geometry("920x620")

        self.connection_var = tk.StringVar(value="UNKNOWN")
        self.mode_var = tk.StringVar(value="GUI")
        self.mapping_var = tk.StringVar(value="0.0 %")
        self.targets_var = tk.StringVar(value="0")
        self.mission_var = tk.StringVar(value="IDLE")
        self.error_var = tk.StringVar(value="-")

        self._log_offset = 0

        self._build_layout()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_layout(self) -> None:
        frame = ttk.Frame(self.root, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)

        status_box = ttk.LabelFrame(frame, text="System Status", padding=10)
        status_box.pack(fill=tk.X, padx=4, pady=4)

        self._status_row(status_box, "Connection", self.connection_var, 0)
        self._status_row(status_box, "Current Mode", self.mode_var, 1)
        self._status_row(status_box, "Mapping Progress", self.mapping_var, 2)
        self._status_row(status_box, "Detected Targets", self.targets_var, 3)
        self._status_row(status_box, "Mission State", self.mission_var, 4)
        self._status_row(status_box, "Last Error", self.error_var, 5)

        controls = ttk.LabelFrame(frame, text="Controls", padding=10)
        controls.pack(fill=tk.X, padx=4, pady=4)

        ttk.Button(controls, text="Start Mapping", command=lambda: self._queue_command("map")).grid(
            row=0, column=0, padx=4, pady=4
        )
        ttk.Button(controls, text="Run Detection", command=lambda: self._queue_command("detect")).grid(
            row=0, column=1, padx=4, pady=4
        )
        ttk.Button(controls, text="Plan Route", command=lambda: self._queue_command("plan")).grid(
            row=0, column=2, padx=4, pady=4
        )
        ttk.Button(controls, text="Start Mission", command=lambda: self._queue_command("start_mission")).grid(
            row=0, column=3, padx=4, pady=4
        )
        ttk.Button(controls, text="Request Status", command=lambda: self._queue_command("status")).grid(
            row=0, column=4, padx=4, pady=4
        )
        ttk.Button(controls, text="Abort", command=lambda: self._queue_command("ABORT")).grid(
            row=0, column=5, padx=4, pady=4
        )

        log_box = ttk.LabelFrame(frame, text="Logs", padding=10)
        log_box.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        self.log_widget = tk.Text(log_box, wrap=tk.WORD, height=18, state=tk.DISABLED)
        self.log_widget.pack(fill=tk.BOTH, expand=True)

    def _status_row(self, parent, label: str, variable: tk.StringVar, row: int) -> None:
        ttk.Label(parent, text=f"{label}:").grid(row=row, column=0, sticky=tk.W, padx=4, pady=2)
        ttk.Label(parent, textvariable=variable).grid(row=row, column=1, sticky=tk.W, padx=4, pady=2)

    def _queue_command(self, command: str) -> None:
        self.controller.submit_command(command, source="gui", wait=False)

    def _refresh(self) -> None:
        status = self.controller.get_status_snapshot()

        self.connection_var.set(status.get("connection_status", "UNKNOWN"))
        self.mode_var.set(status.get("current_mode", "GUI"))
        self.mapping_var.set(f'{status.get("mapping_progress", 0.0):.1f} %')
        self.targets_var.set(str(status.get("detected_targets_count", 0)))
        self.mission_var.set(status.get("mission_state", "IDLE"))
        self.error_var.set(status.get("last_error", "-") or "-")

        self._refresh_logs()
        self.root.after(1000, self._refresh)

    def _refresh_logs(self) -> None:
        total, lines = self.controller.get_recent_logs(self._log_offset)
        if not lines:
            return

        self._log_offset = total
        self.log_widget.configure(state=tk.NORMAL)
        for line in lines:
            self.log_widget.insert(tk.END, line + "\n")
        self.log_widget.see(tk.END)
        self.log_widget.configure(state=tk.DISABLED)

    def _on_close(self) -> None:
        self.root.quit()
        self.root.destroy()

    def run(self) -> None:
        self._refresh()
        self.root.mainloop()
