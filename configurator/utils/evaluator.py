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

# Allowed comparison operators
_ALLOWED_CMP_OPS = {
    ast.Eq: op.eq,
    ast.NotEq: op.ne,
    ast.Lt: op.lt,
    ast.LtE: op.le,
    ast.Gt: op.gt,
    ast.GtE: op.ge,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
}

def safe_eval(expression: str, context: dict):
    """
    Safely evaluates a math/boolean/string expression using AST.
    Supports:
      - numeric and string operations
      - variables from context
      - comparisons: < <= > >= == !=, in, not in
      - boolean ops: and, or, not
      - conditional expression: a if cond else b
    """
    node = ast.parse(expression, mode='eval')

    def _eval(node):
        # Expression root
        if isinstance(node, ast.Expression):
            return _eval(node.body)

        # Numeric / bool / string literal
        if isinstance(node, ast.Constant):
            if isinstance(node.value, (int, float, bool, str)):
                return node.value
            raise ValueError("Only numeric, boolean and string constants allowed.")

        # Variable reference
        if isinstance(node, ast.Name):
            if node.id in context:
                return context[node.id]
            raise ValueError(f"Unknown variable: {node.id}")

        # Binary operations (x + y, x - y, ...)
        if isinstance(node, ast.BinOp):
            if type(node.op) not in _ALLOWED_OPERATORS:
                raise ValueError("Operation not allowed.")
            left = _eval(node.left)
            right = _eval(node.right)
            return _ALLOWED_OPERATORS[type(node.op)](left, right)

        # Unary ops: -x, not x
        if isinstance(node, ast.UnaryOp):
            if isinstance(node.op, ast.USub):
                return -_eval(node.operand)
            if isinstance(node.op, ast.Not):
                return not bool(_eval(node.operand))
            raise ValueError("Unary operator not allowed.")

        # Boolean ops: a and b, a or b
        if isinstance(node, ast.BoolOp):
            if isinstance(node.op, ast.And):
                for value in node.values:
                    if not bool(_eval(value)):
                        return False
                return True
            if isinstance(node.op, ast.Or):
                for value in node.values:
                    if bool(_eval(value)):
                        return True
                return False
            raise ValueError("Boolean operator not allowed.")

        # Comparisons: a < b, a == "x", "a" in b, etc.
        if isinstance(node, ast.Compare):
            left = _eval(node.left)
            for op_node, comparator in zip(node.ops, node.comparators):
                right = _eval(comparator)
                op_type = type(op_node)
                if op_type not in _ALLOWED_CMP_OPS:
                    raise ValueError("Comparison not allowed.")
                if not _ALLOWED_CMP_OPS[op_type](left, right):
                    return False
                left = right  # chain comparisons: a < b < c
            return True

        # Conditional expression: a if cond else b
        if isinstance(node, ast.IfExp):
            condition = bool(_eval(node.test))
            if condition:
                return _eval(node.body)
            else:
                return _eval(node.orelse)

        raise ValueError(f"Unsupported expression: {type(node).__name__}")

    return _eval(node.body)
