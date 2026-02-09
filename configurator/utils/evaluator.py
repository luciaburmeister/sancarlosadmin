import ast
import operator as op
import math
import textwrap

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

# Allowed functions
_ALLOWED_FUNCTIONS = {
    "round": round,       # round(x) or round(x, ndigits)
    "ceil": math.ceil,    # ceil(x)
    "floor": math.floor,  # floor(x)
}


def normalize_expression(expression: str) -> str:
    """
    Normalise a user-entered expression so it's easier to write multi-line
    formulas but still parses reliably:

    - Remove common leading indentation.
    - Normalise newlines (\r\n, \r -> \n).
    - Collapse all whitespace (spaces, tabs, newlines) into single spaces.
    - Strip leading/trailing whitespace.

    This means users can write nicely indented multi-line rules and the
    evaluator/validator always sees a clean one-line expression.
    """
    if expression is None:
        return ""

    # Remove common indent and normalise newlines
    expr = textwrap.dedent(expression)
    expr = expr.replace("\r\n", "\n").replace("\r", "\n").strip()

    # Collapse all whitespace sequences (including newlines) into single spaces
    expr = " ".join(expr.split())

    return expr


def _walk_ast(node: ast.AST, context: dict, *, mode: str):
    """
    Internal recursive AST walker.

    mode:
      - "eval":    actually evaluate using context
      - "validate": only check that the expression uses allowed constructs
                    (no context required; variables are accepted as-is)
    """
    assert mode in ("eval", "validate")

    # Expression root
    if isinstance(node, ast.Expression):
        return _walk_ast(node.body, context, mode=mode)

    # Function calls: round, ceil, floor
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise ValueError("Only simple function calls are allowed (e.g. round(x)).")
        func_name = node.func.id
        if func_name not in _ALLOWED_FUNCTIONS:
            raise ValueError(f"Function not allowed: {func_name}")
        if node.keywords:
            raise ValueError("Keyword arguments are not allowed.")

        # Validate args in both modes
        args = [_walk_ast(arg, context, mode=mode) for arg in node.args]

        if mode == "validate":
            # Don't actually call the function, just structural check
            return None

        func = _ALLOWED_FUNCTIONS[func_name]
        return func(*args)

    # Numeric / bool / string literal
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float, bool, str)):
            # In validate mode we don't care about the value
            return node.value if mode == "eval" else None
        raise ValueError("Only numeric, boolean and string constants are allowed.")

    # Variable reference
    if isinstance(node, ast.Name):
        if mode == "validate":
            # We don't know the context yet; any name is allowed
            return None
        # eval mode → must be present in context
        if node.id in context:
            return context[node.id]
        raise ValueError(f"Unknown variable: {node.id}")

    # Binary operations (x + y, x - y, ...)
    if isinstance(node, ast.BinOp):
        if type(node.op) not in _ALLOWED_OPERATORS:
            raise ValueError("Operation not allowed.")

        if mode == "validate":
            _walk_ast(node.left, context, mode=mode)
            _walk_ast(node.right, context, mode=mode)
            return None

        left = _walk_ast(node.left, context, mode=mode)
        right = _walk_ast(node.right, context, mode=mode)
        return _ALLOWED_OPERATORS[type(node.op)](left, right)

    # Unary ops: -x, not x
    if isinstance(node, ast.UnaryOp):
        if not isinstance(node.op, (ast.USub, ast.Not)):
            raise ValueError("Unary operator not allowed.")

        if mode == "validate":
            _walk_ast(node.operand, context, mode=mode)
            return None

        if isinstance(node.op, ast.USub):
            return -_walk_ast(node.operand, context, mode=mode)
        if isinstance(node.op, ast.Not):
            return not bool(_walk_ast(node.operand, context, mode=mode))

    # Boolean ops: a and b, a or b
    if isinstance(node, ast.BoolOp):
        if not isinstance(node.op, (ast.And, ast.Or)):
            raise ValueError("Boolean operator not allowed.")

        if mode == "validate":
            for v in node.values:
                _walk_ast(v, context, mode=mode)
            return None

        if isinstance(node.op, ast.And):
            # Short-circuit AND
            for value in node.values:
                if not bool(_walk_ast(value, context, mode=mode)):
                    return False
            return True

        if isinstance(node.op, ast.Or):
            # Short-circuit OR
            for value in node.values:
                if bool(_walk_ast(value, context, mode=mode)):
                    return True
            return False

    # Comparisons: a < b, a == "x", "a" in b, etc.
    if isinstance(node, ast.Compare):
        if mode == "validate":
            _walk_ast(node.left, context, mode=mode)
            for op_node, comparator in zip(node.ops, node.comparators):
                if type(op_node) not in _ALLOWED_CMP_OPS:
                    raise ValueError("Comparison not allowed.")
                _walk_ast(comparator, context, mode=mode)
            return None

        left = _walk_ast(node.left, context, mode=mode)
        for op_node, comparator in zip(node.ops, node.comparators):
            if type(op_node) not in _ALLOWED_CMP_OPS:
                raise ValueError("Comparison not allowed.")
            right = _walk_ast(comparator, context, mode=mode)
            if not _ALLOWED_CMP_OPS[type(op_node)](left, right):
                return False
            left = right  # chain comparisons: a < b < c
        return True

    # Conditional expression: a if cond else b
    if isinstance(node, ast.IfExp):
        if mode == "validate":
            _walk_ast(node.test, context, mode=mode)
            _walk_ast(node.body, context, mode=mode)
            _walk_ast(node.orelse, context, mode=mode)
            return None

        condition = bool(_walk_ast(node.test, context, mode=mode))
        if condition:
            return _walk_ast(node.body, context, mode=mode)
        else:
            return _walk_ast(node.orelse, context, mode=mode)

    # Subscript: a[b]
    if isinstance(node, ast.Subscript):
        if mode == "validate":
            _walk_ast(node.value, context, mode=mode)
            _walk_ast(node.slice, context, mode=mode)
            return None

        # eval mode
        target = _walk_ast(node.value, context, mode=mode)
        index = _walk_ast(node.slice, context, mode=mode)
        try:
            return target[index]
        except Exception as e:
            raise ValueError(f"Subscription error: {e}")

    if isinstance(node, ast.Attribute):
        attr_name = node.attr

        # Block all dunder access
        if attr_name.startswith("__"):
            raise ValueError("Access to dunder attributes is not allowed.")

        if mode == "validate":
            _walk_ast(node.value, context, mode=mode)
            return None

        target = _walk_ast(node.value, context, mode=mode)

        if isinstance(target, dict):
            if attr_name in target:
                return target[attr_name]
            raise ValueError(f"Key not found in dict: {attr_name}")

        raise ValueError("Attribute access is only allowed on dictionaries.")

    # Anything else is forbidden
    raise ValueError(f"Unsupported expression element: {type(node).__name__}")

def safe_eval(expression: str, context: dict):
    """
    Safely evaluates a math/boolean/string expression using AST.
    Supports:
      - numeric and string operations
      - variables from context
      - comparisons: < <= > >= == !=, in, not in
      - boolean ops: and, or, not
      - conditional expression: a if cond else b
      - functions: round(x[, ndigits]), ceil(x), floor(x)
    """
    expr = normalize_expression(expression)
    node = ast.parse(expr, mode="eval")
    return _walk_ast(node, context, mode="eval")

def validate_expression_for_safe_eval(expression: str) -> None:
    """
    Validate that 'expression' is:
    - syntactically valid
    - only uses constructs supported by safe_eval

    Does NOT check that all variables exist.
    Raises ValueError on invalid expression.
    """
    expr = normalize_expression(expression)
    try:
        node = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise ValueError(f"Syntax error: {e.msg}")  # nicer than full traceback

    # We don't need a real context for validation
    _walk_ast(node, context={}, mode="validate")
