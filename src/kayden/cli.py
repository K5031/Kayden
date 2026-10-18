from kayden.audio import audio_diagnostics
from kayden.runtime.application import ApplicationRuntime
from kayden.runtime.registry import Registry
import argparse
import warnings

warnings.filterwarnings("ignore")


def _doctor():
    facts = audio_diagnostics()
    print("Kayden voice diagnostics")
    print(f"Microphones: {', '.join(facts['input']) or 'none'}")
    print(f"Default microphone: {facts.get('input_default')}")
    print(f"Outputs: {', '.join(facts['output']) or 'none'}")
    print(f"Default output: {facts.get('output_default')}")
    for error in facts["errors"]:
        print(f"Error: {error}")
    if not facts["input"] and not facts["output"]:
        print("No audio devices are visible to this process.")
        return 1
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="kayden")
    parser.add_argument(
        "command",
        nargs="?",
        choices=("doctor",),
        help="run 'doctor' to inspect voice/audio support",
    )
    args = parser.parse_args(argv)
    if args.command == "doctor":
        return _doctor()
    api = Registry.from_config()
    with ApplicationRuntime(api, owns_registry=True) as runtime:
        runtime.attach_frontend(api.get("frontend"))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
