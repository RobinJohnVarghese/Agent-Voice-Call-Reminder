import re
import string

_formatter = string.Formatter()


def placeholders(template: str) -> set[str]:
    """Return the `{field}` names used in a message template."""
    names = set()
    try:
        for _, field, _, _ in _formatter.parse(template):
            if field:
                names.add(normalize_key(field))
    except ValueError as exc:
        raise ValueError(f"Invalid message template: {exc}") from exc
    return names


def normalize_key(key: str) -> str:
    return re.sub(r"[^0-9a-z]+", "_", str(key).strip().lower()).strip("_")


class _Defaulting(dict):
    def __missing__(self, key):
        return ""


def render(template: str, values: dict[str, str]) -> str:
    normalized = _Defaulting({normalize_key(k): ("" if v is None else str(v)) for k, v in values.items()})
    out = []
    for literal, field, spec, conv in _formatter.parse(template):
        out.append(literal)
        if field:
            out.append(normalized[normalize_key(field)])
    return "".join(out)
