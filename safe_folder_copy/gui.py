"""The one window people actually see. Everything it says is in plain Indonesian."""

from __future__ import annotations

import os
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import __version__
from .core import Progress, Result, copy_folder, format_size, target_for

GREEN = "#1b7f3b"
ORANGE = "#b25e00"
RED = "#b3261e"
GREY = "#5f6368"


def _shorten(text: str, width: int = 90) -> str:
    return text if len(text) <= width else "…" + text[-(width - 1):]


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"Safe Folder Copy {__version__}")
        self.minsize(680, 0)
        self.resizable(True, False)

        self.source = tk.StringVar()
        self.destination = tk.StringVar()
        self.target_text = tk.StringVar(value=" ")
        self.status = tk.StringVar(value="Pilih folder sumber dan tujuan, lalu tekan Mulai Salin.")
        self.detail = tk.StringVar(value=" ")

        self._cancel = threading.Event()
        self._worker: threading.Thread | None = None
        self._progress: Progress | None = None
        self._outcome: tuple[str, object] | None = None
        self._result: Result | None = None
        self._target: Path | None = None
        self._closing = False

        self._build()
        self.source.trace_add("write", lambda *_: self._update_target())
        self.destination.trace_add("write", lambda *_: self._update_target())
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    @property
    def busy(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    def _build(self) -> None:
        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        bold = ("Segoe UI", 10, "bold")

        ttk.Label(frame, text="1.  Folder yang mau disalin", font=bold).grid(
            row=0, column=0, columnspan=2, sticky="w"
        )
        ttk.Entry(frame, textvariable=self.source).grid(row=1, column=0, sticky="ew", pady=(2, 10))
        self.source_button = ttk.Button(frame, text="Pilih…", command=self._pick_source)
        self.source_button.grid(row=1, column=1, padx=(8, 0), pady=(2, 10))

        ttk.Label(frame, text="2.  Simpan salinannya di", font=bold).grid(
            row=2, column=0, columnspan=2, sticky="w"
        )
        ttk.Entry(frame, textvariable=self.destination).grid(row=3, column=0, sticky="ew", pady=(2, 2))
        self.destination_button = ttk.Button(frame, text="Pilih…", command=self._pick_destination)
        self.destination_button.grid(row=3, column=1, padx=(8, 0), pady=(2, 2))
        ttk.Label(frame, textvariable=self.target_text, foreground=GREY).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(0, 10)
        )

        ttk.Label(
            frame,
            text=(
                "Aman: folder sumber hanya dibaca, tidak ada yang diubah atau dihapus di sana. "
                "Berkas yang sudah ada di tujuan tidak akan ditimpa. Setiap berkas diperiksa "
                "ulang setelah disalin."
            ),
            foreground=GREY,
            wraplength=640,
            justify="left",
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(0, 12))

        buttons = ttk.Frame(frame)
        buttons.grid(row=6, column=0, columnspan=2, sticky="w")
        self.start_button = ttk.Button(buttons, text="3.  Mulai Salin", command=self._start)
        self.start_button.pack(side="left", ipadx=12, ipady=4)
        self.cancel_button = ttk.Button(
            buttons, text="Batalkan", command=self._request_cancel, state="disabled"
        )
        self.cancel_button.pack(side="left", padx=8, ipady=4)

        self.bar = ttk.Progressbar(frame, mode="determinate", maximum=100)
        self.bar.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(14, 6))
        self.status_label = ttk.Label(frame, textvariable=self.status, font=bold, wraplength=640)
        self.status_label.grid(row=8, column=0, columnspan=2, sticky="w")
        ttk.Label(frame, textvariable=self.detail, foreground=GREY).grid(
            row=9, column=0, columnspan=2, sticky="w", pady=(2, 8)
        )

        self.summary = tk.Text(frame, height=8, wrap="word", relief="flat", background="#f6f6f6")
        self.summary.grid(row=10, column=0, columnspan=2, sticky="ew")
        self.summary.configure(state="disabled")

        after = ttk.Frame(frame)
        after.grid(row=11, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self.report_button = ttk.Button(
            after, text="Buka laporan", command=self._open_report, state="disabled"
        )
        self.report_button.pack(side="left")
        self.folder_button = ttk.Button(
            after, text="Buka folder hasil salinan", command=self._open_target, state="disabled"
        )
        self.folder_button.pack(side="left", padx=8)

    def _pick_source(self) -> None:
        chosen = filedialog.askdirectory(title="Pilih folder yang mau disalin", mustexist=True)
        if chosen:
            self.source.set(os.path.normpath(chosen))

    def _pick_destination(self) -> None:
        chosen = filedialog.askdirectory(title="Pilih tempat menyimpan salinannya")
        if chosen:
            self.destination.set(os.path.normpath(chosen))

    def _update_target(self) -> None:
        source, destination = self.source.get().strip(), self.destination.get().strip()
        if source and destination:
            self.target_text.set(f"Hasil salinan akan berada di:  {target_for(source, destination)}")
        else:
            self.target_text.set(" ")

    def _set_summary(self, text: str) -> None:
        self.summary.configure(state="normal")
        self.summary.delete("1.0", "end")
        self.summary.insert("1.0", text)
        self.summary.configure(state="disabled")

    def _lock(self, running: bool) -> None:
        state = "disabled" if running else "normal"
        for widget in (self.start_button, self.source_button, self.destination_button):
            widget.configure(state=state)
        self.cancel_button.configure(state="normal" if running else "disabled")

    def _start(self) -> None:
        source, destination = self.source.get().strip(), self.destination.get().strip()
        if not source or not destination:
            messagebox.showinfo("Safe Folder Copy", "Pilih folder sumber dan folder tujuan dulu.")
            return
        target = target_for(source, destination)
        if os.path.normcase(str(target)) == os.path.normcase(os.path.abspath(source)):
            messagebox.showwarning(
                "Safe Folder Copy",
                "Tujuan yang dipilih adalah tempat folder sumber itu sendiri berada.\n\n"
                "Pilih drive atau folder lain sebagai tujuan.",
            )
            return

        self._target = target
        self._cancel.clear()
        self._progress = None
        self._outcome = None
        self._set_summary("")
        self.report_button.configure(state="disabled")
        self.folder_button.configure(state="disabled")
        self.status_label.configure(foreground="")
        self.status.set("Memeriksa isi folder…")
        self.detail.set(" ")
        self.bar.configure(mode="indeterminate")
        self.bar.start(12)
        self._lock(True)

        self._worker = threading.Thread(
            target=self._work, args=(Path(source), target), daemon=True
        )
        self._worker.start()
        self.after(100, self._poll)

    def _work(self, source: Path, target: Path) -> None:
        def report(progress: Progress) -> None:
            self._progress = Progress(**vars(progress))

        try:
            self._outcome = ("done", copy_folder(source, target, self._cancel, report))
        except ValueError as exc:
            self._outcome = ("refused", str(exc))
        except Exception as exc:  # show it instead of leaving a window that never finishes
            self._outcome = ("crashed", f"{type(exc).__name__}: {exc}")

    def _request_cancel(self) -> None:
        self._cancel.set()
        self.cancel_button.configure(state="disabled")
        self.status.set("Membatalkan… menunggu berkas yang sedang disalin dibereskan.")

    def _poll(self) -> None:
        progress = self._progress
        if progress is not None and not self._cancel.is_set():
            if progress.phase == "scan":
                self.status.set(f"Memeriksa isi folder… {progress.files_total:,} berkas ditemukan")
                self.detail.set(_shorten(progress.current))
            else:
                if str(self.bar["mode"]) == "indeterminate":
                    self.bar.stop()
                    self.bar.configure(mode="determinate")
                if progress.bytes_total:
                    self.bar["value"] = progress.bytes_done * 100 / progress.bytes_total
                elif progress.files_total:
                    self.bar["value"] = progress.files_done * 100 / progress.files_total
                self.status.set(
                    f"Menyalin {progress.files_done:,} dari {progress.files_total:,} berkas  •  "
                    f"{format_size(progress.bytes_done)} dari {format_size(progress.bytes_total)}"
                )
                self.detail.set(_shorten(progress.current))

        if self.busy:
            self.after(100, self._poll)
            return
        if self._closing:
            self.destroy()
            return
        self._finish()

    def _finish(self) -> None:
        self.bar.stop()
        self.bar.configure(mode="determinate")
        self._lock(False)
        self.detail.set(" ")
        kind, payload = self._outcome or ("crashed", "proses berhenti tanpa hasil")

        if kind == "refused":
            self.bar["value"] = 0
            self.status.set("Belum dimulai.")
            messagebox.showwarning("Safe Folder Copy", str(payload))
            return
        if kind == "crashed":
            self.bar["value"] = 0
            self.status_label.configure(foreground=RED)
            self.status.set("Terjadi galat yang tidak terduga.")
            self._set_summary(
                f"{payload}\n\nFolder sumber tidak diubah. Berkas yang sudah tersalin aman."
            )
            return

        result: Result = payload
        self._result = result
        not_copied = len(result.unreadable) + len(result.failed)
        if result.stopped_reason:
            colour, headline = RED, result.stopped_reason
        elif result.cancelled:
            colour = ORANGE
            headline = "Dibatalkan. Yang sudah tersalin aman; tekan Mulai Salin lagi untuk melanjutkan."
        elif result.complete:
            colour, headline = GREEN, "Selesai. Semua berkas tersalin dan sudah diperiksa ulang."
            self.bar["value"] = 100
        else:
            colour = ORANGE
            headline = f"Selesai, tetapi {not_copied + len(result.conflicts)} berkas tidak tersalin. Lihat laporan."
            self.bar["value"] = 100
        self.status_label.configure(foreground=colour)
        self.status.set(headline)

        lines = [
            f"Disalin dan diperiksa ulang   : {result.copied:,} berkas ({format_size(result.copied_bytes)})",
            f"Sudah ada di tujuan, isi sama : {result.already_same:,} berkas",
        ]
        if result.conflicts:
            lines.append(f"Dibiarkan, di tujuan berbeda  : {len(result.conflicts):,} berkas")
        if result.unreadable:
            lines.append(f"Tidak bisa dibaca dari sumber : {len(result.unreadable):,}")
        if result.failed:
            lines.append(f"Gagal disalin                 : {len(result.failed):,}")
        for problem in (result.unreadable + result.failed)[:5]:
            lines.append(f"   • {_shorten(problem.path, 60)} — {problem.reason}")
        if not_copied > 5:
            lines.append(f"   … dan {not_copied - 5} lainnya, lihat laporan.")
        lines.append("")
        lines.append("Folder sumber tidak diubah sama sekali.")
        self._set_summary("\n".join(lines))

        if result.report_path:
            self.report_button.configure(state="normal")
        if self._target and os.path.isdir(self._target):
            self.folder_button.configure(state="normal")

    def _open_report(self) -> None:
        if self._result and self._result.report_path:
            os.startfile(self._result.report_path)  # type: ignore[attr-defined]

    def _open_target(self) -> None:
        if self._target:
            os.startfile(self._target)  # type: ignore[attr-defined]

    def _on_close(self) -> None:
        if not self.busy:
            self.destroy()
            return
        if messagebox.askyesno(
            "Safe Folder Copy",
            "Penyalinan masih berjalan.\n\nBatalkan dan tutup? Berkas yang sudah tersalin tetap aman.",
        ):
            self._closing = True
            self._request_cancel()


def _sharp_text_on_high_dpi() -> None:
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass  # not Windows, or an old one; the window just looks slightly blurry


def run_app() -> None:
    _sharp_text_on_high_dpi()
    App().mainloop()
