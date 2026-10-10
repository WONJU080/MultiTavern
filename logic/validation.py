"""Shared validation for untrusted WebSocket text fields."""


def clean_optional_text(value: object, field: str, maximum: int) -> str:
    """Return a stripped string within the length limit, or an empty string for None."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"'{field}' 必须是字符串")
    cleaned = value.strip()
    if len(cleaned) > maximum:
        raise ValueError(f"'{field}' 不能超过 {maximum} 个字符")
    return cleaned


def clean_text(value: object, field: str, maximum: int) -> str:
    """Return a stripped, non-empty string within the length limit."""
    if not isinstance(value, str):
        raise ValueError(f"'{field}' 必须是字符串")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > maximum:
        raise ValueError(f"'{field}' 必须是 1-{maximum} 个字符")
    return cleaned
