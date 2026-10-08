"""AURA AI desktop launcher.

Global hotkey: Ctrl+Alt+A
The process is intended to run in the Windows background/startup.
It opens a small GUI that accepts both typed and spoken commands.
"""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import messagebox
import traceback

import aura_terminal
from app.voice_control import listen_and_transcribe, speak_text

HOTKEY = "ctrl+alt+a"


class AuraWindow:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("AURA AI")
        self.root.geometry("620x430")
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self.hide)

        title = tk.Label(root, text="AURA AI", font=("Segoe UI", 22, "bold"))
        title.pack(pady=(18, 4))
        subtitle = tk.Label(
            root,
            text="Local AI Desktop Agent • Text + Voice",
            font=("Segoe UI", 10),
        )
        subtitle.pack(pady=(0, 14))

        self.status = tk.StringVar(value="Ready")
        tk.Label(root, textvariable=self.status, font=("Segoe UI", 10)).pack()

        self.entry = tk.Entry(root, font=("Segoe UI", 13), width=58)
        self.entry.pack(padx=20, pady=14)
        self.entry.bind("<Return>", lambda _event: self.run_text())

        buttons = tk.Frame(root)
        buttons.pack(pady=4)

        tk.Button(
            buttons, text="Run Text", width=14, height=2, command=self.run_text
        ).grid(row=0, column=0, padx=6)

        tk.Button(
            buttons, text="🎤 Voice", width=14, height=2, command=self.run_voice
        ).grid(row=0, column=1, padx=6)

        tk.Button(
            buttons, text="Hide", width=14, height=2, command=self.hide
        ).grid(row=0, column=2, padx=6)

        self.output = tk.Text(
            root, height=11, width=70, font=("Consolas", 10), state="disabled"
        )
        self.output.pack(padx=20, pady=14)

        tk.Label(
            root,
            text="Global shortcut: Ctrl + Alt + A",
            font=("Segoe UI", 9),
        ).pack(pady=(0, 8))

    def log(self, text: str):
        self.output.configure(state="normal")
        self.output.insert("end", text + "\n")
        self.output.see("end")
        self.output.configure(state="disabled")

    def show(self):
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(250, lambda: self.root.attributes("-topmost", False))
        self.entry.focus_force()

    def hide(self):
        self.root.withdraw()

    def run_text(self):
        command = self.entry.get().strip()
        if not command:
            return
        self.entry.delete(0, "end")
        self.log(f"YOU > {command}")
        self.status.set("Working...")
        try:
            keep = aura_terminal.handle_command(command)
            self.status.set("Ready")
            self.log("AURA > Done" if keep else "AURA > Stopped")
            if not keep:
                self.hide()
        except Exception as exc:
            self.status.set("Error")
            self.log(f"AURA ERROR > {exc}")
            traceback.print_exc()

    def run_voice(self):
        self.status.set("Listening...")
        self.log("AURA > Listening...")

        def worker():
            try:
                result = listen_and_transcribe(seconds=6, language="auto")
                command = str(result.get("text", "")).strip()
                self.root.after(0, lambda: self._voice_result(command))
            except Exception as exc:
                self.root.after(0, lambda: self._voice_error(str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _voice_result(self, command: str):
        if not command:
            self.status.set("Ready")
            self.log("AURA > I did not hear a command.")
            return

        self.log(f"YOU 🎤 > {command}")
        self.status.set("Working...")
        try:
            keep = aura_terminal.handle_command(command)
            self.status.set("Ready")
            response = "Done." if keep else "AURA stopped."
            self.log("AURA > " + response)
            try:
                threading.Thread(target=speak_text, args=(response,), daemon=True).start()
            except Exception:
                pass
            if not keep:
                self.hide()
        except Exception as exc:
            self.status.set("Error")
            self.log(f"AURA ERROR > {exc}")

    def _voice_error(self, error: str):
        self.status.set("Ready")
        self.log(f"AURA VOICE ERROR > {error}")
        messagebox.showerror("AURA Voice", error)


def main():
    try:
        import keyboard
    except ImportError:
        raise SystemExit(
            "keyboard is missing. Run: python -m pip install keyboard"
        )

    root = tk.Tk()
    window = AuraWindow(root)
    window.hide()

    def activate():
        root.after(0, window.show)

    keyboard.add_hotkey(HOTKEY, activate, suppress=False)
    print(f"AURA background agent ready. Press {HOTKEY.upper()} to open it.")

    root.mainloop()


if __name__ == "__main__":
    main()
