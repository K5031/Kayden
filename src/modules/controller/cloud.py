from interfaces import ControllerInterface
from config import load_system_prompt
from events import TextDelta, UsageReported
from turns import build_turn


class Module(ControllerInterface):
    required_modules = ["input", "output", "core", "context", "memory", "action", "tools"]
    required_features = {
        "core": {"runtime_kind": "cloud", "reports_usage": True},
        "context": {"usage_mode": "reported"},
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
        print(f"Selected model: {core.model}")

        try:
            while True:
                user_input = inp.get_input()
                output.interrupt()

                memories = memory.retrieve(user_input)
                context = contextMgr.get()
                turn = build_turn(system_prompt, context, memories, user_input)
                reply = ""
                prompt_tokens = None

                try:
                    for event in action.run(
                        core,
                        turn,
                        tools,
                        should_interrupt=inp.has_input,
                    ):
                        if isinstance(event, TextDelta):
                            output.send(event.text)
                            reply += event.text
                        elif isinstance(event, UsageReported):
                            reported = event.usage.get("prompt_tokens")
                            if isinstance(reported, int):
                                prompt_tokens = reported
                    if inp.has_input():
                        output.interrupt()
                        print("\n[interrupted]")
                    else:
                        output.flush()
                except KeyboardInterrupt:
                    core.interrupt()
                    output.interrupt()
                    print("\n[interrupted]")

                if isinstance(prompt_tokens, int):
                    contextMgr.report_usage(prompt_tokens)

                contextMgr.add("user", user_input)
                contextMgr.add("assistant", reply)
                memory.store([
                    {"role": "user", "content": user_input},
                    {"role": "assistant", "content": reply}
                ])

        except (KeyboardInterrupt, EOFError):
            print("\nExiting.")
