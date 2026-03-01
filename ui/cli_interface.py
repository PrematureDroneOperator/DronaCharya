import json


class CLIInterface:
    VALID_COMMANDS = {"map", "detect", "plan", "start_mission", "status", "exit"}

    def __init__(self, controller) -> None:
        self.controller = controller

    def run(self) -> None:
        print("")
        print("dronAcharya CLI commands: map | detect | plan | start_mission | status | exit")

        while True:
            raw = input("dronAcharya> ").strip().lower()
            if not raw:
                continue
            if raw not in self.VALID_COMMANDS:
                print(f"Unknown command: {raw}")
                continue
            if raw == "exit":
                print("Exiting CLI.")
                break

            result = self.controller.submit_command(raw, source="cli", wait=True)
            print(result.get("message", ""))

            if raw == "status":
                status = result.get("status") or self.controller.get_status_snapshot()
                print(json.dumps(status, indent=2))
