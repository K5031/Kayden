import unittest

from kayden.modules.tools.calculator import Module as CalculatorTool


class CalculatorToolTests(unittest.TestCase):
    def test_evaluates_arithmetic_and_functions(self):
        self.assertEqual(CalculatorTool().execute("calculate", {"expression": "sqrt(81) + 2 ** 3"}), "17.0")

    def test_rejects_code_and_excessive_calculations(self):
        calculator = CalculatorTool()
        with self.assertRaisesRegex(ValueError, "unsupported expression"):
            calculator.execute("calculate", {"expression": "__import__('os').getcwd()"})
        with self.assertRaisesRegex(ValueError, "exponent is too large"):
            calculator.execute("calculate", {"expression": "2 ** 1001"})


if __name__ == "__main__":
    unittest.main()
