import io

import pandas as pd
import pytest

from app.services.recipients import RecipientFileError, normalize_phone, parse_recipients
from app.services.templating import placeholders, render


def test_parse_csv_normalizes_dedupes_and_reports_errors():
    content = b"Name,Mobile\nAsha,+91 98765 43210\nRahul,09876501234\nDup,919876543210\nBad,123\n,\n"
    result = parse_recipients("list.csv", content, "IN", 100)
    assert [r.phone_number for r in result.recipients] == ["+919876543210", "+919876501234"]
    assert result.recipients[0].name == "Asha"
    assert result.duplicates == 1
    assert result.errors == [{"row": 5, "error": "invalid phone number '123'"}]


def test_parse_excel_with_numeric_phone_and_extra_columns():
    buffer = io.BytesIO()
    pd.DataFrame({"phone": [919876543210], "Due Date": ["1 Dec"]}).to_excel(buffer, index=False)
    result = parse_recipients("list.xlsx", buffer.getvalue(), "IN", 100)
    assert result.recipients[0].phone_number == "+919876543210"
    assert result.recipients[0].variables["due_date"] == "1 Dec"


def test_missing_phone_column():
    with pytest.raises(RecipientFileError, match="No phone column"):
        parse_recipients("list.csv", b"name,email\nA,a@x.com\n", "IN", 100)


def test_unsupported_file_type():
    with pytest.raises(RecipientFileError, match="Unsupported"):
        parse_recipients("list.pdf", b"x", "IN", 100)


def test_row_limit():
    with pytest.raises(RecipientFileError, match="maximum"):
        parse_recipients("list.csv", b"phone\n9876543210\n9876543211\n", "IN", 1)


def test_normalize_phone_international():
    assert normalize_phone("+1 (415) 555-2671", "IN") == "+14155552671"
    assert normalize_phone("0044 20 7946 0958", "IN") == "+442079460958"


def test_templating():
    assert placeholders("Hi {Name}, see you on {Appointment Date}") == {"name", "appointment_date"}
    assert render("Hi {name}, on {appointment_date}{missing}", {"Name": "Asha", "appointment date": "1 Dec"}) == (
        "Hi Asha, on 1 Dec"
    )
