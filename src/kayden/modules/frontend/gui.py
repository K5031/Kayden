from kayden.ui.tk import TkFrontend


class Module(TkFrontend):
    """Full desktop chat frontend."""

    def __init__(self):
        super().__init__(title="Kayden", geometry="860x680")
