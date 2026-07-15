from __future__ import annotations

import ast
import math
import operator

from skills import ResourceLimits


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


def _evaluate(node: ast.AST) -> int | float:
    if isinstance(node, ast.Expression):
        return _evaluate(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPERATORS:
        return _UNARY_OPERATORS[type(node.op)](_evaluate(node.operand))
    if isinstance(node, ast.BinOp) and type(node.op) in _BINARY_OPERATORS:
        left = _evaluate(node.left)
        right = _evaluate(node.right)
        if isinstance(node.op, ast.Pow):
            if abs(right) > ResourceLimits.MAX_EXPONENT:
                raise ValueError(f"exponent magnitude must not exceed {ResourceLimits.MAX_EXPONENT}")
            if abs(left) > 1e6 and abs(right) > 5:
                raise ValueError("large base with large exponent is not allowed")
        result = _BINARY_OPERATORS[type(node.op)](left, right)
        if isinstance(result, complex) or not math.isfinite(float(result)) or abs(result) > 1e100:
            raise ValueError("calculation result is out of range")
        return result
    raise ValueError(f"unsupported expression element: {type(node).__name__}")


def calculator(expression: str) -> dict:
    """计算数学表达式（带资源限制）。

    Args:
        expression: 数学表达式字符串，如 "23 * 17 + 9"

    Returns:
        业务结果字典
    """
    if not isinstance(expression, str) or not expression.strip():
        raise ValueError("expression must be a non-empty string")
    if len(expression) > ResourceLimits.MAX_EXPRESSION_LENGTH:
        raise ValueError(f"expression is too long (max {ResourceLimits.MAX_EXPRESSION_LENGTH} chars)")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ValueError("invalid arithmetic expression") from exc
    return {"result": _evaluate(tree)}
