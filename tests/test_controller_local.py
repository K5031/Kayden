import unittest
from unittest.mock import patch

from kayden.interfaces import ControllerInterface
from kayden.modules.controller.local import Module as LocalController


class LocalControllerTests(unittest.TestCase):
    def test_declares_local_compatibility(self):
        controller = LocalController()
        self.assertIsInstance(controller, ControllerInterface)
        self.assertEqual(controller.required_features, {
            "core": {"runtime_kind": "local"},
            "context": {"usage_mode": "self"},
        })

    def test_runs_frontend_controller_without_reported_usage(self):
        api = object()
        with patch("kayden.modules.controller.local.run_frontend_controller") as run:
            LocalController().run(api)
        run.assert_called_once_with(api)


if __name__ == "__main__":
    unittest.main()
