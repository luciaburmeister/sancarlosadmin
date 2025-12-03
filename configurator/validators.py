import ast
from django.core.exceptions import ValidationError

ALLOWED_VARIABLES = {"base_total", "material_cost", "quantity", "item_count"}

ALLOWED_NODES = (
    ast.Expression, ast.BinOp, ast.UnaryOp,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.Mod,
    ast.USub, ast.Constant, ast.Name, ast.Load
)

def pricing_expression_validator(expression: str):
    """
    Function-based validator (fully serializable by Django).
    """
    try:
        node = ast.parse(expression, mode="eval")
    except SyntaxError as e:
        raise ValidationError(f"Invalid syntax: {e}")

    for child in ast.walk(node):
        if not isinstance(child, ALLOWED_NODES):
            raise ValidationError(
                f"Invalid element '{type(child).__name__}'. Only math expressions allowed."
            )

        if isinstance(child, ast.Name) and child.id not in ALLOWED_VARIABLES:
            raise ValidationError(
                f"Unknown variable '{child.id}'. Allowed: {', '.join(ALLOWED_VARIABLES)}"
            )

        if isinstance(child, (ast.Call, ast.Attribute, ast.Subscript)):
            raise ValidationError("Function calls, attributes, or indexing not allowed.")
