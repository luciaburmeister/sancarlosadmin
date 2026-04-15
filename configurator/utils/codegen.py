from django.db import models
import re

def next_scoped_code(model, scope_filter, prefix, width=5, separator="-", field_name="code"):
    """
    Generates the next sequential code for a model in a given scope.
    Format: {prefix}{separator}{0000N}
    """
    regex = rf"^{re.escape(prefix)}{re.escape(separator)}(\d+)$"
    
    # Get all existing codes in the scope
    existing_codes = model.objects.filter(**scope_filter).values_list(field_name, flat=True)
    
    max_num = 0
    for code in existing_codes:
        if not code:
            continue
        match = re.match(regex, code)
        if match:
            num = int(match.group(1))
            if num > max_num:
                max_num = num
                
    next_num = max_num + 1
    return f"{prefix}{separator}{next_num:0{width}d}"
