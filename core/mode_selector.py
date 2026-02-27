from __future__ import annotations


def select_mode() -> str:
    while True:
        print("")
        print("Select startup mode:")
        print("1. GUI Mode")
        print("2. CLI Mode")
        choice = input("Enter choice [1/2]: ").strip()

        if choice == "1":
            return "gui"
        if choice == "2":
            return "cli"
        print("Invalid option. Please choose 1 or 2.")
