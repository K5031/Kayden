import signal
from interfaces import ControllerInterface
from config import load_system_prompt
from events import TextDelta
from turns import build_turn


class Module(ControllerInterface):
    required_modules = ["input", "output", "core", "context", "memory", "action", "tools"]
    required_features = {
        "core": {"runtime_kind": "local"},
        "context": {"usage_mode": "self"},
    }

    def run(self, api) -> None:
        inp = api.get("input")
        output = api.get("output")
        core = api.get("core")
        contextMgr = api.get("context")
        memory = api.get("memory")
        action = api.get("action")
        tools = api.get("tools")

        system_prompt = load_system_prompt()
        print(f"Selected model: {core.model_path}")

        try:
            while True:
                user_input = inp.get_input()
                output.interrupt()

                memories = memory.retrieve(user_input)
                context = contextMgr.get()
                turn = build_turn(system_prompt, context, memories, user_input)
                reply = ""

                interrupted = {"flag": False}

                def _on_sigint(signum, frame):
                    interrupted["flag"] = True

                previous_handler = signal.signal(signal.SIGINT, _on_sigint)
                try:
                    def should_interrupt():
                        return inp.has_input() or interrupted["flag"]

                    for event in action.run(
                        core,
                        turn,
                        tools,
                        should_interrupt=should_interrupt,
                    ):
                        if isinstance(event, TextDelta):
                            output.send(event.text)
                            reply += event.text
                    if should_interrupt():
                        output.interrupt()
                        interrupted["flag"] = False
                        print("\n[interrupted]")
                    else:
                        output.flush()
                finally:
                    signal.signal(signal.SIGINT, previous_handler)

                contextMgr.add("user", user_input)
                contextMgr.add("assistant", reply)
                memory.store([
                    {"role": "user", "content": user_input},
                    {"role": "assistant", "content": reply}
                ])

        except (KeyboardInterrupt, EOFError):
            print("\nExiting.")
