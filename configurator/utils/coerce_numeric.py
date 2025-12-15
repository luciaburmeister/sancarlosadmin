def _coerce_numeric(value):
    """
    Try to convert string values to numbers.
    - "10"    -> 10
    - "10.5"  -> 10.5
    - "10,5"  -> 10.5  (comma to dot)
    Non-numeric strings are returned unchanged.
    Non-strings are returned unchanged.
    """
    if isinstance(value, str):
        raw = value.strip().replace(",", ".")
        try:
            num = float(raw)
        except ValueError:
            return value  # keep as string if not numeric

        # If it is an integer value (e.g. 10.0) return int
        if num.is_integer():
            return int(num)
        return num
    return value
