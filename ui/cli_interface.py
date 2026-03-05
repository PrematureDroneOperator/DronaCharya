import json


class CLIInterface:
    VALID_COMMANDS = {
        "start_survey",
        "stop_survey",
        "start_recording",
        "stop_recording",
        "build_route",
        "start_mission",
        "status",
        "abort",
        "map",
        "detect",
        "plan",
        "exit",
    }

    def __init__(self, controller) -> None:
        self.controller = controller

    def run(self) -> None:
        print("")
        print("dronAcharya CLI commands: start_survey | stop_survey | start_recording | stop_recording | build_route | start_mission | status | abort | exit")
        print("Backward aliases: map -> start_survey, detect/plan -> build_route")

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
