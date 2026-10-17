from kayden.interfaces import OutputInterface


class Module(OutputInterface):
    def __init__(self):
        self._at_line_start = True

    def send(self, token: str) -> None:
        print(token, end="", flush=True)
        if token:
            self._at_line_start = token.endswith("\n")

    def status(self, message: str) -> None:
        if not self._at_line_start:
            print()
        print(message, flush=True)
        self._at_line_start = True

    def interrupt(self):
        pass

    def flush(self):
        pass
