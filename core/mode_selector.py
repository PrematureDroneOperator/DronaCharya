def select_mode() -> str:
    gui_available = True
    try:
        import tkinter  # noqa: F401
    except Exception:
        gui_available = False

    while True:
        print("")
        print("Select startup mode:")
        if gui_available:
            print("1. GUI Mode")
        else:
            print("1. GUI Mode (unavailable: tkinter missing)")
        print("2. CLI Mode")
        choice = input("Enter choice [1/2]: ").strip()

        if choice == "1" and gui_available:
            return "gui"
        if choice == "1" and not gui_available:
            print("GUI mode cannot start because tkinter is not available.")
            continue
        if choice == "2":
            return "cli"
        print("Invalid option. Please choose 1 or 2.")
