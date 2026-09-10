from django import template

register = template.Library()


@register.filter
def get_field(form, field_name):
    """
    Returns the bound field `form[field_name]`. Used to render a form field
    whose name is dynamic (e.g. "price_123") and can't be referenced with
    the usual `{{ form.field_name }}` template syntax.
    """
    try:
        return form[field_name]
    except KeyError:
        return ""