import ast
import operator as op

# Allowed math operations
_ALLOWED_OPERATORS = {
    ast.Add: op.add,
    ast.Sub: op.sub,
    ast.Mult: op.mul,
    ast.Div: op.truediv,
    ast.Pow: op.pow,
    ast.Mod: op.mod,
}

def safe_eval(expression: str, context: dict):
    """
    Safely evaluates a math expression using AST.
    Only allows numeric operations + variables in context.
    """
    node = ast.parse(expression, mode='eval')

    def _eval(node):
        # Expression root
        if isinstance(node, ast.Expression):
            return _eval(node.body)

        # Numeric literal
        if isinstance(node, ast.Constant):  # Python 3.8+
            if isinstance(node.value, (int, float)):
                return node.value
            raise ValueError("Only numeric constants allowed.")

        # Variable reference
        if isinstance(node, ast.Name):
            if node.id in context:
                return context[node.id]
            raise ValueError(f"Unknown variable: {node.id}")

        # Binary operations (x + y)
        if isinstance(node, ast.BinOp):
            if type(node.op) not in _ALLOWED_OPERATORS:
                raise ValueError("Operation not allowed.")
            left = _eval(node.left)
            right = _eval(node.right)
            return _ALLOWED_OPERATORS[type(node.op)](left, right)

        # Unary ops: -x
        if isinstance(node, ast.UnaryOp):
            if isinstance(node.op, ast.USub):
                return -_eval(node.operand)
            raise ValueError("Unary operator not allowed.")

        raise ValueError(f"Unsupported expression: {type(node).__name__}")

    return _eval(node.body)
