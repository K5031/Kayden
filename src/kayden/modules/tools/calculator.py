import ast
import math
import operator

from kayden.interfaces import ToolInterface

_BINARY_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPERATORS = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_CONSTANTS = {"pi": math.pi, "e": math.e, "tau": math.tau}
_FUNCTIONS = {
    "abs": abs,
    "ceil": math.ceil,
    "cos": math.cos,
    "floor": math.floor,
    "log": math.log,
    "log10": math.log10,
    "max": max,
    "min": min,
    "round": round,
    "sin": math.sin,
    "sqrt": math.sqrt,
    "tan": math.tan,
}


def _check_result(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("expression must produce a real number")
    if isinstance(value, int) and value.bit_length() > 4096:
        raise ValueError("result is too large")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("result is not finite")
    return value


def _evaluate(node):
    if isinstance(node, ast.Expression):
        return _evaluate(node.body)
    if isinstance(node, ast.Constant):
        return _check_result(node.value)
    if isinstance(node, ast.Name) and node.id in _CONSTANTS:
        return _CONSTANTS[node.id]
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPERATORS:
        return _check_result(_UNARY_OPERATORS[type(node.op)](_evaluate(node.operand)))
    if isinstance(node, ast.BinOp) and type(node.op) in _BINARY_OPERATORS:
        left = _evaluate(node.left)
        right = _evaluate(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 1000:
            raise ValueError("exponent is too large")
        return _check_result(_BINARY_OPERATORS[type(node.op)](left, right))
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in _FUNCTIONS
        and not node.keywords
    ):
        arguments = [_evaluate(argument) for argument in node.args]
        return _check_result(_FUNCTIONS[node.func.id](*arguments))
    raise ValueError("unsupported expression")


class Module(ToolInterface):
    def schemas(self) -> list[dict]:
        return [{
            "type": "function",
            "function": {
                "name": "calculate",
                "description": (
                    "Safely evaluate arithmetic with common mathematical functions "
                    "and the constants pi, e, and tau."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "expression": {
                            "type": "string",
                            "description": "The arithmetic expression to evaluate.",
                        },
                    },
                    "required": ["expression"],
                    "additionalProperties": False,
                },
            },
        }]

    def execute(self, name: str, arguments: dict) -> str:
        if name != "calculate":
            raise ValueError(f"Unknown tool: {name}")
        if set(arguments) != {"expression"}:
            raise ValueError("calculate requires only an expression")
        expression = arguments["expression"]
        if not isinstance(expression, str) or not expression.strip():
            raise ValueError("expression must be a non-empty string")
        if len(expression) > 200:
            raise ValueError("expression is too long")
        try:
            tree = ast.parse(expression, mode="eval")
            result = _evaluate(tree)
        except (SyntaxError, ArithmeticError, TypeError) as error:
            raise ValueError(f"invalid calculation: {error}") from error
        return str(result)
