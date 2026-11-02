from pathlib import Path

import pymupdf
import pytest

from llm_pdf_ocr.pdf import (
    EncryptedPdfError,
    InvalidPdfError,
    PageText,
    TextLayerProblem,
    assess_text_layer,
    read_pdf,
)

FIXTURES = Path(__file__).parent / "fixtures"


def read_fixture(name: str) -> list[PageText]:
    return read_pdf((FIXTURES / name).read_bytes())


def assert_in_order(text: str, *parts: str) -> None:
    positions = [text.index(part) for part in parts]
    assert positions == sorted(positions), f"out of order: {parts} at {positions}"


# Born-digital


def test_born_digital_every_page_has_a_usable_text_layer():
    pages = read_fixture("born_digital.pdf")

    assert [p.number for p in pages] == [1, 2]
    assert all(p.usable for p in pages)
    assert all(p.problem is None for p in pages)


def test_born_digital_two_columns_read_title_then_left_then_right():
    text = read_fixture("born_digital.pdf")[0].text

    assert_in_order(
        text,
        "Quarterly Report",
        "Left column first paragraph.",
        "Left column second paragraph.",
        "Right column first paragraph.",
        "Right column second paragraph.",
    )


def test_born_digital_paragraph_lines_stay_together():
    text = read_fixture("born_digital.pdf")[0].text

    assert "Revenue grew in\nevery region" in text
    assert "\n\nLeft column second paragraph." in text


def test_born_digital_label_value_rows_read_across():
    text = read_fixture("born_digital.pdf")[1].text

    assert_in_order(text, "Single column paragraph.", "Invoice", "Date", "Total")
    assert "Invoice INV-0042" in text
    assert "Date 2024-03-01" in text
    assert "Total 1,250.00" in text


# Scanned


def test_scanned_image_only_page_has_no_text_layer():
    page = read_fixture("scanned.pdf")[0]

    assert page.usable is False
    assert page.problem is TextLayerProblem.NO_TEXT
    assert page.text == ""


def test_scanned_page_with_only_a_watermark_is_not_usable():
    page = read_fixture("scanned.pdf")[1]

    assert page.usable is False
    assert page.problem is TextLayerProblem.IMAGE_WITH_LITTLE_TEXT
    assert page.text == ""


# Mixed


def test_mixed_pdf_decides_each_page_on_its_own():
    pages = read_fixture("mixed.pdf")

    assert [(p.number, p.usable, p.problem) for p in pages] == [
        (1, True, None),
        (2, False, TextLayerProblem.NO_TEXT),
        (3, False, TextLayerProblem.GARBLED_TEXT),
        (4, True, None),
    ]


def test_mixed_pdf_searchable_scan_uses_its_invisible_ocr_text():
    page = read_fixture("mixed.pdf")[3]

    assert page.text.startswith("Searchable scan text.")
    assert "even though the page is a picture." in page.text


def test_mixed_pdf_born_digital_page_is_extracted():
    page = read_fixture("mixed.pdf")[0]

    assert "Invoice INV-0042" in page.text


# Text-layer heuristic


@pytest.mark.parametrize(
    ("text", "coverage", "problem"),
    [
        ("", 0.0, TextLayerProblem.NO_TEXT),
        ("  \n\t ", 0.0, TextLayerProblem.NO_TEXT),
        ("���� text here ��", 0.0, TextLayerProblem.GARBLED_TEXT),
        (" abc", 0.0, TextLayerProblem.GARBLED_TEXT),
        ("~ ' . , ^ ; : _ | . ' ` ~", 0.0, TextLayerProblem.GARBLED_TEXT),
        ("Scanned by CamScanner", 0.9, TextLayerProblem.IMAGE_WITH_LITTLE_TEXT),
        ("Scanned by CamScanner", 0.2, None),
        ("Thank you.", 0.0, None),
        ("Données: 12,5 % — façade, Straße, 東京", 0.0, None),
        ("word " * 20, 1.0, None),
    ],
)
def test_assess_text_layer(text, coverage, problem):
    assert assess_text_layer(text, coverage) is problem


# Bad input


def test_not_a_pdf_is_rejected():
    with pytest.raises(InvalidPdfError):
        read_pdf(b"this is not a pdf")


def test_empty_bytes_are_rejected():
    with pytest.raises(InvalidPdfError):
        read_pdf(b"")


def test_encrypted_pdf_is_rejected():
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "secret")
    data = doc.tobytes(
        encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="user"
    )

    with pytest.raises(EncryptedPdfError):
        read_pdf(data)


def test_pdf_errors_share_a_base_class():
    from llm_pdf_ocr.pdf import PdfReadError

    assert issubclass(InvalidPdfError, PdfReadError)
    assert issubclass(EncryptedPdfError, PdfReadError)
