"""Optional standalone AURA voice mode.

For the no-terminal experience, prefer aura_desktop.py + Ctrl+Alt+A.
This file is kept for direct voice testing.
"""

from app.voice_control import listen_and_transcribe, speak_text
from aura_terminal import handle_command, EXIT_WORDS


def main():
    print("AURA Voice Mode")
    print("Speak a command. Say 'exit' to stop.")
    while True:
        try:
            result = listen_and_transcribe(seconds=6, language="auto")
            command = str(result.get("text", "")).strip()
            if not command:
                print("AURA > I did not hear a command.")
                continue
            print(f"YOU 🎤 > {command}")
            if command.casefold() in EXIT_WORDS:
                speak_text("AURA stopped.")
                break
            keep = handle_command(command)
            speak_text("Done." if keep else "AURA stopped.")
            if not keep:
                break
        except KeyboardInterrupt:
            break
        except Exception as exc:
            print(f"AURA Voice Error > {exc}")


if __name__ == "__main__":
    main()
