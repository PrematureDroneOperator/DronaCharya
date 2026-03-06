def select_mode() -> str:
    gui_available = True
    try:
        import tkinter  # noqa: F401
    except Exception:
        gui_available = False
    print("")
    print("[AeroVision] : Welcome to DC-1, Have a nice day!")
    return "cli"

