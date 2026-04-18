#!/usr/bin/env python3
"""AI Video Translator — Graphical User Interface.

A friendly tkinter GUI that wraps the dub_video.sh pipeline so users
don't need the command line.
"""

from __future__ import annotations

import os
import subprocess
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

SCRIPT_DIR = Path(__file__).resolve().parent
DUB_SCRIPT = SCRIPT_DIR / "dub_video.sh"

LANGUAGES = {
    "Portuguese (Brazil)": "pt-BR",
    "Spanish": "es",
    "French": "fr",
    "German": "de",
    "Italian": "it",
}

STEP_LABELS = [
    "Setup environment",
    "Extract audio",
    "Transcribe (Whisper)",
    "Translate subtitles",
    "Generate dubbed audio",
    "Merge final video",
]

# ── Colours & style constants ─────────────────────────────────────────────
BG = "#1e1e2e"
BG_CARD = "#2a2a3c"
FG = "#cdd6f4"
FG_DIM = "#6c7086"
ACCENT = "#89b4fa"
ACCENT_HOVER = "#74c7ec"
SUCCESS = "#a6e3a1"
ERR = "#f38ba8"
FONT_FAMILY = "Helvetica"


# ── Step indicator widget ─────────────────────────────────────────────────

class StepIndicator(tk.Frame):
    """A single row showing a step number, label, and status icon."""

    STATUS_CHARS = {"pending": "○", "running": "◉", "done": "✓", "error": "✗"}
    STATUS_COLORS = {"pending": FG_DIM, "running": ACCENT, "done": SUCCESS, "error": ERR}

    def __init__(self, master: tk.Widget, number: int, label: str, **kw) -> None:
        super().__init__(master, bg=BG_CARD, **kw)
        self._status = "pending"

        self.icon_var = tk.StringVar(value=self.STATUS_CHARS["pending"])
        self.icon_label = tk.Label(
            self,
            textvariable=self.icon_var,
            font=(FONT_FAMILY, 16),
            fg=FG_DIM,
            bg=BG_CARD,
            width=2,
        )
        self.icon_label.pack(side=tk.LEFT, padx=(8, 0))

        self.text_label = tk.Label(
            self,
            text=f"Step {number}:  {label}",
            font=(FONT_FAMILY, 13),
            fg=FG_DIM,
            bg=BG_CARD,
            anchor="w",
        )
        self.text_label.pack(side=tk.LEFT, padx=(4, 8), fill=tk.X, expand=True)

    def set_status(self, status: str) -> None:
        self._status = status
        self.icon_var.set(self.STATUS_CHARS.get(status, "○"))
        color = self.STATUS_COLORS.get(status, FG_DIM)
        self.icon_label.configure(fg=color)
        fg = FG if status in ("running", "done") else FG_DIM
        self.text_label.configure(fg=fg)

    def reset(self) -> None:
        self.set_status("pending")


# ── Main application ──────────────────────────────────────────────────────

class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("AI Video Translator")
        self.configure(bg=BG)
        self.minsize(640, 860)
        self.resizable(True, True)

        self._running = False
        self._process: subprocess.Popen | None = None

        self._build_ui()
        self._center_window()

    # ── Layout ────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # Header
        header = tk.Frame(self, bg=BG)
        header.pack(fill=tk.X, padx=24, pady=(24, 0))
        tk.Label(
            header,
            text="🌐  AI Video Translator",
            font=(FONT_FAMILY, 22, "bold"),
            fg=FG,
            bg=BG,
        ).pack(anchor="w")
        tk.Label(
            header,
            text="Dub any video into another language automatically.",
            font=(FONT_FAMILY, 12),
            fg=FG_DIM,
            bg=BG,
        ).pack(anchor="w", pady=(2, 0))

        # ── Card 1: Select video ──────────────────────────────────────────
        card_input = self._card("① Select a video file")
        row = tk.Frame(card_input, bg=BG_CARD)
        row.pack(fill=tk.X, padx=16, pady=(0, 12))

        self.file_var = tk.StringVar(value="No file selected")
        tk.Label(
            row,
            textvariable=self.file_var,
            font=(FONT_FAMILY, 11),
            fg=FG,
            bg=BG_CARD,
            anchor="w",
        ).pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.browse_btn = tk.Button(
            row,
            text="Browse…",
            font=(FONT_FAMILY, 11),
            bg=ACCENT,
            fg=BG,
            activebackground=ACCENT_HOVER,
            activeforeground=BG,
            relief=tk.FLAT,
            padx=14,
            pady=4,
            cursor="hand2",
            command=self._browse_file,
        )
        self.browse_btn.pack(side=tk.RIGHT, padx=(8, 0))

        # ── Card 2: LLM API settings ─────────────────────────────────────
        card_llm = self._card("② LLM API settings (for translation)")

        # API Endpoint
        ep_row = tk.Frame(card_llm, bg=BG_CARD)
        ep_row.pack(fill=tk.X, padx=16, pady=(0, 6))
        tk.Label(ep_row, text="Endpoint:", font=(FONT_FAMILY, 11), fg=FG_DIM, bg=BG_CARD, width=9, anchor="w").pack(side=tk.LEFT)
        self.api_base_var = tk.StringVar(value="http://localhost:8000")
        tk.Entry(ep_row, textvariable=self.api_base_var, font=(FONT_FAMILY, 11), bg="#181825", fg=FG, insertbackground=FG, relief=tk.FLAT, highlightthickness=1, highlightbackground="#313244").pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=3)

        # API Key
        key_row = tk.Frame(card_llm, bg=BG_CARD)
        key_row.pack(fill=tk.X, padx=16, pady=(0, 6))
        tk.Label(key_row, text="API Key:", font=(FONT_FAMILY, 11), fg=FG_DIM, bg=BG_CARD, width=9, anchor="w").pack(side=tk.LEFT)
        self.api_key_var = tk.StringVar(value="apikey")
        tk.Entry(key_row, textvariable=self.api_key_var, font=(FONT_FAMILY, 11), bg="#181825", fg=FG, insertbackground=FG, relief=tk.FLAT, show="*", highlightthickness=1, highlightbackground="#313244").pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=3)

        # Model
        model_row = tk.Frame(card_llm, bg=BG_CARD)
        model_row.pack(fill=tk.X, padx=16, pady=(0, 12))
        tk.Label(model_row, text="Model:", font=(FONT_FAMILY, 11), fg=FG_DIM, bg=BG_CARD, width=9, anchor="w").pack(side=tk.LEFT)
        self.model_var = tk.StringVar(value="")
        model_entry = tk.Entry(model_row, textvariable=self.model_var, font=(FONT_FAMILY, 11), bg="#181825", fg=FG, insertbackground=FG, relief=tk.FLAT, highlightthickness=1, highlightbackground="#313244")
        model_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=3)
        tk.Label(model_row, text="(blank = auto-detect)", font=(FONT_FAMILY, 9), fg=FG_DIM, bg=BG_CARD).pack(side=tk.LEFT, padx=(6, 0))

        # ── Card 3: Choose language ───────────────────────────────────────
        card_lang = self._card("③ Choose target language")
        lang_row = tk.Frame(card_lang, bg=BG_CARD)
        lang_row.pack(fill=tk.X, padx=16, pady=(0, 12))

        self.lang_var = tk.StringVar(value=list(LANGUAGES.keys())[0])
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(
            "Custom.TCombobox",
            fieldbackground="#313244",
            background="#45475a",
            foreground=FG,
            arrowcolor=ACCENT,
            selectbackground=ACCENT,
            selectforeground="#1e1e2e",
            padding=6,
        )
        style.map(
            "Custom.TCombobox",
            fieldbackground=[("readonly", "#313244")],
            foreground=[("readonly", FG)],
        )
        # Style the dropdown list
        self.option_add("*TCombobox*Listbox.background", "#313244")
        self.option_add("*TCombobox*Listbox.foreground", FG)
        self.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.option_add("*TCombobox*Listbox.selectForeground", "#1e1e2e")
        self.option_add("*TCombobox*Listbox.font", (FONT_FAMILY, 12))
        self.lang_combo = ttk.Combobox(
            lang_row,
            textvariable=self.lang_var,
            values=list(LANGUAGES.keys()),
            state="readonly",
            font=(FONT_FAMILY, 12),
            style="Custom.TCombobox",
        )
        self.lang_combo.pack(fill=tk.X)

        # ── Card 4: Pipeline progress ─────────────────────────────────────
        card_prog = self._card("④ Pipeline progress", expand=True)
        self.steps: list[StepIndicator] = []
        for i, label in enumerate(STEP_LABELS, start=1):
            si = StepIndicator(card_prog, i, label)
            si.pack(fill=tk.X, padx=12, pady=2)
            self.steps.append(si)

        # Log area
        log_frame = tk.Frame(card_prog, bg=BG_CARD)
        log_frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=(8, 12))

        self.log_text = tk.Text(
            log_frame,
            height=8,
            font=("Menlo", 10),
            bg="#181825",
            fg=FG_DIM,
            insertbackground=FG,
            relief=tk.FLAT,
            wrap=tk.WORD,
            state=tk.DISABLED,
            highlightthickness=0,
        )
        scrollbar = tk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # ── Action buttons ────────────────────────────────────────────────
        btn_row = tk.Frame(self, bg=BG)
        btn_row.pack(fill=tk.X, padx=24, pady=(8, 24))

        self.start_btn = tk.Button(
            btn_row,
            text="▶  Start Dubbing",
            font=(FONT_FAMILY, 14, "bold"),
            bg=ACCENT,
            fg=BG,
            activebackground=ACCENT_HOVER,
            activeforeground=BG,
            relief=tk.FLAT,
            padx=20,
            pady=10,
            cursor="hand2",
            command=self._on_start,
        )
        self.start_btn.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.cancel_btn = tk.Button(
            btn_row,
            text="Cancel",
            font=(FONT_FAMILY, 12),
            bg="#45475a",
            fg=FG,
            activebackground="#585b70",
            activeforeground=FG,
            relief=tk.FLAT,
            padx=16,
            pady=10,
            cursor="hand2",
            state=tk.DISABLED,
            command=self._on_cancel,
        )
        self.cancel_btn.pack(side=tk.RIGHT, padx=(12, 0))

    # ── Widget helpers ────────────────────────────────────────────────────

    def _card(self, title: str, expand: bool = False) -> tk.Frame:
        wrapper = tk.Frame(self, bg=BG)
        wrapper.pack(fill=tk.BOTH, expand=expand, padx=24, pady=(16, 0))
        tk.Label(
            wrapper,
            text=title,
            font=(FONT_FAMILY, 13, "bold"),
            fg=FG,
            bg=BG,
            anchor="w",
        ).pack(anchor="w", pady=(0, 6))
        card = tk.Frame(
            wrapper, bg=BG_CARD, highlightthickness=1, highlightbackground="#313244"
        )
        card.pack(fill=tk.BOTH, expand=expand)
        # top padding inside card
        tk.Frame(card, bg=BG_CARD, height=10).pack()
        return card

    def _center_window(self) -> None:
        self.update_idletasks()
        w, h = self.winfo_width(), self.winfo_height()
        x = (self.winfo_screenwidth() - w) // 2
        y = (self.winfo_screenheight() - h) // 3
        self.geometry(f"+{x}+{y}")

    def _log(self, text: str) -> None:
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, text + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _clear_log(self) -> None:
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state=tk.DISABLED)

    # ── User actions ──────────────────────────────────────────────────────

    def _browse_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Select a video file",
            filetypes=[
                ("Video files", "*.mp4 *.mkv *.avi *.mov *.webm"),
                ("All files", "*.*"),
            ],
        )
        if path:
            self.file_var.set(path)

    def _set_running(self, running: bool) -> None:
        self._running = running
        normal = tk.NORMAL if not running else tk.DISABLED
        cancel = tk.NORMAL if running else tk.DISABLED
        self.start_btn.configure(state=normal)
        self.browse_btn.configure(state=normal)
        self.lang_combo.configure(state="readonly" if not running else tk.DISABLED)
        self.cancel_btn.configure(state=cancel)

    def _on_start(self) -> None:
        video_path = self.file_var.get()
        if not video_path or video_path == "No file selected":
            messagebox.showwarning("No file", "Please select a video file first.")
            return
        if not Path(video_path).is_file():
            messagebox.showerror("File not found", f"Could not find:\n{video_path}")
            return

        lang_display = self.lang_var.get()
        lang_code = LANGUAGES.get(lang_display, "pt-BR")

        for step in self.steps:
            step.reset()
        self._clear_log()
        self._set_running(True)

        thread = threading.Thread(
            target=self._run_pipeline,
            args=(video_path, lang_code),
            daemon=True,
        )
        thread.start()

    def _on_cancel(self) -> None:
        if self._process and self._process.poll() is None:
            self._process.terminate()
            self.after(0, self._log, "⚠  Pipeline cancelled by user.")
        self.after(0, self._set_running, False)

    # ── Pipeline runner (background thread) ───────────────────────────────

    _STEP_TRIGGERS = ["Step 0/5", "Step 1/5", "Step 2/5", "Step 3/5", "Step 4/5", "Step 5/5"]

    def _detect_step(self, line: str) -> int | None:
        for i, trigger in enumerate(self._STEP_TRIGGERS):
            if trigger in line:
                return i
        return None

    def _run_pipeline(self, video_path: str, lang_code: str) -> None:
        cmd = [
            "bash",
            str(DUB_SCRIPT),
            "--input", video_path,
            "--language", lang_code,
            "--force",
        ]

        env = os.environ.copy()
        env["LLM_API_BASE"] = self.api_base_var.get().strip()
        env["LLM_API_KEY"] = self.api_key_var.get().strip()
        model = self.model_var.get().strip()
        if model:
            env["LLM_MODEL"] = model

        current_step = -1
        try:
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=env,
                bufsize=1,
            )

            for raw_line in iter(self._process.stdout.readline, ""):
                line = raw_line.rstrip()
                if not line:
                    continue

                detected = self._detect_step(line)
                if detected is not None:
                    if current_step >= 0:
                        self.after(0, self.steps[current_step].set_status, "done")
                    current_step = detected
                    self.after(0, self.steps[current_step].set_status, "running")

                self.after(0, self._log, line)

            self._process.wait()
            rc = self._process.returncode

            if rc == 0:
                if current_step >= 0:
                    self.after(0, self.steps[current_step].set_status, "done")
                self.after(0, self._log, "")
                self.after(0, self._log, "✅  Done! Your dubbed video is ready.")
                self.after(
                    0,
                    lambda: messagebox.showinfo(
                        "Success",
                        "Your dubbed video has been created!\n\n"
                        f"Look for the .{lang_code}.synced.mp4 file\n"
                        "next to the original video.",
                    ),
                )
            else:
                if current_step >= 0:
                    self.after(0, self.steps[current_step].set_status, "error")
                self.after(0, self._log, f"\n❌  Pipeline failed (exit code {rc}).")

        except Exception as exc:
            self.after(0, self._log, f"\n❌  Error: {exc}")
            if current_step >= 0:
                self.after(0, self.steps[current_step].set_status, "error")
        finally:
            self.after(0, self._set_running, False)


if __name__ == "__main__":
    app = App()
    app.mainloop()
