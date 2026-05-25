from datetime import date


def default_config_name(template):
    return f"{template.name} - {date.today().strftime('%d/%m')}"


def missing_required_params(template, param_values):
    missing = []
    values = param_values or {}
    for p in template.parameters.all():
        if not p.required:
            continue
        if p.name not in values:
            missing.append(p.label)
            continue
        v = values.get(p.name)
        if v is None:
            missing.append(p.label)
            continue
        if p.type in {"text", "number", "select"} and str(v).strip() == "":
            missing.append(p.label)
    return missing
