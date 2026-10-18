import unittest

from kayden.runtime.tools import ToolRuntime


class _Tool:
    def __init__(self, name="echo", result="ok", error=None):
        self.name = name
        self.result = result
        self.error = error
        self.stopped = False

    def schemas(self):
        return [{"type": "function", "function": {
            "name": self.name, "parameters": {"type": "object"},
        }}]

    def execute(self, name, arguments):
        if self.error:
            raise self.error
        return self.result

    def stop(self):
        self.stopped = True


class ToolRuntimeTests(unittest.TestCase):
    def test_combines_schemas_and_routes_execution(self):
        runtime = ToolRuntime([_Tool("first", "one"), _Tool("second", "two")])
        self.assertEqual([s["function"]["name"] for s in runtime.schemas()], ["first", "second"])
        self.assertEqual(runtime.execute("second", {}).content, "two")

    def test_rejects_duplicate_names(self):
        with self.assertRaisesRegex(ValueError, "duplicate tool name"):
            ToolRuntime([_Tool("same"), _Tool("same")])

    def test_unknown_and_failed_tools_become_error_results(self):
        runtime = ToolRuntime([_Tool(error=ValueError("bad input"))])
        self.assertTrue(runtime.execute("missing", {}).is_error)
        result = runtime.execute("echo", {})
        self.assertTrue(result.is_error)
        self.assertIn("bad input", result.content)

    def test_stop_reaches_owned_modules(self):
        first, second = _Tool("first"), _Tool("second")
        runtime = ToolRuntime([first, second])
        runtime.stop()
        self.assertTrue(first.stopped and second.stopped)

    def test_added_tools_are_not_owned(self):
        runtime = ToolRuntime([])
        borrowed = _Tool("borrowed")
        runtime.add(borrowed)
        runtime.stop()
        self.assertEqual(runtime.execute("borrowed", {}).content, "ok")
        self.assertFalse(borrowed.stopped)

    def test_replaces_borrowed_tools_atomically(self):
        runtime = ToolRuntime([_Tool("owned")])
        old = _Tool("memory", "old")
        new = _Tool("memory", "new")
        runtime.add(old)

        runtime.replace_borrowed(old, new)

        self.assertEqual(runtime.execute("memory", {}).content, "new")
        self.assertTrue(runtime.execute("owned", {}).content)

    def test_failed_borrowed_replacement_preserves_existing_routes(self):
        runtime = ToolRuntime([_Tool("owned")])
        old = _Tool("memory", "old")
        runtime.add(old)

        with self.assertRaisesRegex(ValueError, "duplicate tool name"):
            runtime.replace_borrowed(old, _Tool("owned", "conflict"))

        self.assertEqual(runtime.execute("memory", {}).content, "old")


if __name__ == "__main__":
    unittest.main()
