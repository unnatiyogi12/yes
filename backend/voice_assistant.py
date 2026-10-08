"""Run AURA AI as a local voice assistant.

Start FastAPI separately:
python -m uvicorn app.main:app --reload --port 8000

Then run:
python voice_assistant.py
"""

import requests

API = "http://127.0.0.1:8000/voice/command"
DEVICES_API = "http://127.0.0.1:8000/voice/devices"


def main():
    print("AURA Voice Mode")
    print("Choose your microphone once, then search or give commands repeatedly.")
    print()

    device = None
    try:
        response = requests.get(DEVICES_API, timeout=10)
        response.raise_for_status()
        device_data = response.json()
        devices = device_data.get("devices", [])
        default_device = device_data.get("default_device")
        if devices:
            print("Available microphones:")
            for item in devices:
                default_marker = " (Windows default)" if item["index"] == default_device else ""
                print(f"  {item['index']}: {item['name']}{default_marker}")
            selection = input(
                "Enter the microphone number to use "
                "(press ENTER for Windows default): "
            ).strip()
            if selection:
                device = int(selection)
                if device not in {item["index"] for item in devices}:
                    raise ValueError("Choose one of the listed microphone numbers.")
    except (requests.RequestException, ValueError) as exc:
        print(f"Microphone selection unavailable: {exc}")
        print("Continuing with the Windows default microphone.")
        device = None

    while True:
        try:
            input("Press ENTER, then speak for up to 8 seconds (Ctrl+C to exit)... ")

            response = requests.post(
                API,
                json={"seconds": 8, "language": "auto", "device": device},
                timeout=120,
            )
            response.raise_for_status()
            data = response.json()

            print("\nAURA:", data)

            spoken = str(data.get("input", "")).strip().casefold()
            if spoken in {"exit", "quit", "stop aura", "close aura"}:
                break

            if data.get("audio_level"):
                print("Recorded microphone level:", data["audio_level"])

        except KeyboardInterrupt:
            print("\nVoice mode stopped.")
            break
        except Exception as exc:
            print("Voice error:", exc)


if __name__ == "__main__":
    main()
