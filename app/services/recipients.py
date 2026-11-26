import io
from dataclasses import dataclass, field

import pandas as pd
import phonenumbers

from app.services.templating import normalize_key

PHONE_COLUMNS = (
    "phone", "phone_number", "phone_no", "mobile", "mobile_number", "mobile_no", "number",
    "contact", "contact_number", "contact_no", "msisdn", "to",
)
NAME_COLUMNS = ("name", "full_name", "recipient", "recipient_name", "customer_name", "customer")


class RecipientFileError(ValueError):
    pass


@dataclass
class Recipient:
    phone_number: str
    name: str | None
    variables: dict[str, str]


@dataclass
class ParseResult:
    recipients: list[Recipient] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    duplicates: int = 0
    columns: list[str] = field(default_factory=list)


def _detect_delimiter(content: bytes) -> str:
    first_line = content.decode("utf-8-sig", errors="ignore").splitlines()[0] if content.strip() else ""
    counts = {d: first_line.count(d) for d in (",", ";", "\t", "|")}
    best = max(counts, key=counts.get)
    return best if counts[best] else ","


def _read_frame(filename: str, content: bytes) -> pd.DataFrame:
    name = (filename or "").lower()
    buffer = io.BytesIO(content)
    try:
        if name.endswith((".xlsx", ".xlsm", ".xls")):
            return pd.read_excel(buffer, dtype=str)
        if name.endswith((".csv", ".txt")):
            return pd.read_csv(buffer, dtype=str, encoding="utf-8-sig", sep=_detect_delimiter(content))
    except Exception as exc:
        raise RecipientFileError(f"Could not read file: {exc}") from exc
    raise RecipientFileError("Unsupported file type. Upload a .csv or .xlsx file.")


def _clean(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip()
    if text.lower() == "nan":
        return ""
    return text


def normalize_phone(raw: str, default_region: str) -> str:
    text = raw.strip()
    if text.endswith(".0"):
        text = text[:-2]
    if text.startswith("00"):
        text = "+" + text[2:]
    digits = "".join(ch for ch in text if ch.isdigit())
    if not text.startswith("+") and default_region:
        country_code = str(phonenumbers.country_code_for_region(default_region.upper()))
        if len(digits) > 10 and digits.startswith(country_code):
            text = "+" + digits
    try:
        parsed = phonenumbers.parse(text, None if text.startswith("+") else default_region.upper())
    except phonenumbers.NumberParseException as exc:
        raise ValueError(f"invalid phone number '{raw}'") from exc
    if not phonenumbers.is_valid_number(parsed):
        raise ValueError(f"invalid phone number '{raw}'")
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def parse_recipients(filename: str, content: bytes, default_region: str, max_rows: int) -> ParseResult:
    frame = _read_frame(filename, content)
    frame = frame.dropna(how="all")
    columns = [normalize_key(c) for c in frame.columns]
    if len(set(columns)) != len(columns):
        raise RecipientFileError("Duplicate column names in file.")
    frame.columns = columns

    phone_col = next((c for c in PHONE_COLUMNS if c in columns), None)
    if not phone_col:
        raise RecipientFileError(
            "No phone column found. Add a column named one of: " + ", ".join(PHONE_COLUMNS[:6])
        )
    name_col = next((c for c in NAME_COLUMNS if c in columns), None)

    if len(frame) > max_rows:
        raise RecipientFileError(f"File has {len(frame)} rows; the maximum is {max_rows}.")

    result = ParseResult(columns=columns)
    seen: set[str] = set()
    for index, row in enumerate(frame.to_dict(orient="records"), start=2):
        raw_phone = _clean(row.get(phone_col))
        if not raw_phone:
            result.errors.append({"row": index, "error": "missing phone number"})
            continue
        try:
            phone = normalize_phone(raw_phone, default_region)
        except ValueError as exc:
            result.errors.append({"row": index, "error": str(exc)})
            continue
        if phone in seen:
            result.duplicates += 1
            continue
        seen.add(phone)
        variables = {k: _clean(v) for k, v in row.items()}
        variables["phone"] = phone
        name = _clean(row.get(name_col)) if name_col else ""
        if name_col:
            variables["name"] = name
        result.recipients.append(Recipient(phone_number=phone, name=name or None, variables=variables))
    return result
