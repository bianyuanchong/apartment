from django import template

register = template.Library()


@register.filter
def usd(value):
    """Format a number as USD with thousands separators (absolute value)."""
    if value is None or value == "":
        return "—"
    try:
        n = abs(int(round(float(value))))
    except (TypeError, ValueError):
        return "—"
    return f"${n:,}"
