from modules.frontend._tk import TkFrontend


class Module(TkFrontend):
    """Compact always-on-top assistant frontend."""

    def __init__(self):
        super().__init__(title="Kayden · Floaty", geometry="520x330", floating=True)
