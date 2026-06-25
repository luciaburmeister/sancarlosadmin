from django.db import models
import re


def next_scoped_code(model, scope_filter, prefix, width=5, separator="-", field_name="code"):
    """
    Generates the next sequential code for a model in a given scope.
    Format: {prefix}{separator}{0000N}

    The lookup searches by **prefix** across the entire company (or whatever
    the real uniqueness scope is), not just by item_type, because the DB
    unique constraint is (company, code) — independent of item_type.
    """
    # Build a company-only filter: take 'company' from scope_filter,
    # then match codes starting with the prefix via SQL LIKE.
    company_filter = {}
    if "company" in scope_filter:
        company_filter["company"] = scope_filter["company"]
    elif "company_id" in scope_filter:
        company_filter["company_id"] = scope_filter["company_id"]

    like_pattern = f"{prefix}{separator}"
    qs = (
        model.objects
        .filter(**company_filter, **{f"{field_name}__startswith": like_pattern})
        .select_for_update()                      # prevent race conditions
        .values_list(field_name, flat=True)
    )

    regex = rf"^{re.escape(prefix)}{re.escape(separator)}(\d+)$"
    max_num = 0
    for code in qs:
        if not code:
            continue
        match = re.match(regex, code)
        if match:
            num = int(match.group(1))
            if num > max_num:
                max_num = num

    next_num = max_num + 1
    return f"{prefix}{separator}{next_num:0{width}d}"
