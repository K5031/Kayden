import threading
import unittest

from kayden.permissions import (
    InvocationContext,
    OperationDefinition,
    PermissionBroker,
    PermissionDenied,
)


class PermissionBrokerTests(unittest.TestCase):
    def test_allows_and_denies_without_prompting(self):
        prompts = []
        broker = PermissionBroker(ask=lambda request: prompts.append(request))
        context = InvocationContext(origin="model")

        broker.require(
            OperationDefinition("tool.clock", "Read the clock"),
            {},
            context,
        )
        with self.assertRaisesRegex(PermissionDenied, "tool.files.delete"):
            broker.require(
                OperationDefinition(
                    "tool.files.delete",
                    "Delete a file",
                    default_decision="deny",
                ),
                {"path": "/tmp/example"},
                context,
            )

        self.assertEqual(prompts, [])

    def test_ask_is_one_shot_and_captures_exact_arguments_and_origin(self):
        captured = []

        def ask(request):
            captured.append(request)
            return request.arguments == {"key": "profile.name"}

        broker = PermissionBroker(ask=ask)
        operation = OperationDefinition(
            "tool.forget_memory",
            "Forget profile.name",
            effects=frozenset({"memory.delete"}),
            default_decision="ask",
        )
        arguments = {"key": "profile.name"}
        context = InvocationContext(origin="model", explicit=False)

        broker.require(operation, arguments, context)
        arguments["key"] = "profile.university"

        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].arguments, {"key": "profile.name"})
        self.assertEqual(captured[0].context, context)
        self.assertEqual(captured[0].operation.effects, frozenset({"memory.delete"}))

    def test_runtime_policy_overrides_provider_default_by_exact_operation(self):
        broker = PermissionBroker(rules={"tool.forget_memory": "deny"})
        operation = OperationDefinition(
            "tool.forget_memory",
            "Forget memory",
            default_decision="allow",
        )

        with self.assertRaises(PermissionDenied):
            broker.require(
                operation,
                {"key": "profile.name"},
                InvocationContext(origin="user", explicit=True),
            )

    def test_request_ids_are_unique_across_concurrent_prompts(self):
        requests = []
        lock = threading.Lock()

        def ask(request):
            with lock:
                requests.append(request)
            return True

        broker = PermissionBroker(ask=ask)
        operation = OperationDefinition(
            "tool.test.ask",
            "Test",
            default_decision="ask",
        )
        threads = [threading.Thread(
            target=broker.require,
            args=(operation, {"value": index}, InvocationContext("model")),
        ) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len({request.request_id for request in requests}), 8)


if __name__ == "__main__":
    unittest.main()
