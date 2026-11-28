"""Pin the web page's design tokens to the shared demo-shell spec table.

The spec is fleet-deploy `docs/demo-shell.md`: the "Tokens" section (the shared
table and the per-app accent table, ocr column), the "Spacing scale" section
(spacing, radius, type, focus ring) and "Layout tokens". Every shared token must
stay identical across translate, pdfchat and ocr; only the accent column differs.
"""

import re
from pathlib import Path

import pytest

STYLE = Path(__file__).resolve().parents[1] / "src" / "llm_pdf_ocr" / "web" / "style.css"

# Spec "Tokens -> Shared (identical in all three apps)": token -> (light, dark).
SHARED_COLOURS = {
    "--color-bg": ("#f4f5f7", "#0f1115"),
    "--color-surface": ("#ffffff", "#171a20"),
    "--color-surface-sunken": ("#eef0f3", "#1f232b"),
    "--color-text": ("#15171c", "#e6e8ec"),
    "--color-muted": ("#545a65", "#a1a7b3"),
    "--color-border": ("#dcdfe5", "#2c313a"),
    "--color-border-strong": ("#7c8390", "#6f7785"),
    "--color-on-primary": ("#ffffff", "#ffffff"),
    "--color-disabled": ("#e4e7ec", "#2a2f38"),
    "--color-on-disabled": ("#545a65", "#a1a7b3"),
    "--color-danger": ("#b3261e", "#ff8a80"),
    "--color-danger-subtle": ("#fdecea", "#3a1d1b"),
    "--color-warn": ("#8a4a00", "#f0a640"),
    "--color-shadow": ("rgb(16 24 40 / 0.08)", "rgb(0 0 0 / 0.45)"),
}

# Spec "Tokens -> Accent": the ocr column (violet), the only per-app colours.
OCR_ACCENT_COLOURS = {
    "--color-accent": ("#6d28d9", "#c4b5fd"),
    "--color-accent-hover": ("#5b21b6", "#ddd6fe"),
    "--color-accent-subtle": ("#f1ecfd", "#2a2140"),
    "--color-on-accent": ("#ffffff", "#0b1220"),
    "--color-primary": ("#6d28d9", "#7c3aed"),
    "--color-primary-hover": ("#5b21b6", "#6d28d9"),
    "--color-focus": ("#6d28d9", "#c4b5fd"),
}

# Colours this app adds on top of the spec (the find-in-result highlights).
APP_COLOURS = {"--color-find", "--color-on-find"}

# Spec "Spacing scale", radius, type and focus-ring numbers, plus "Layout tokens".
SCALAR_TOKENS = {
    "--space-1": "0.25rem",
    "--space-2": "0.5rem",
    "--space-3": "0.75rem",
    "--space-4": "1rem",
    "--space-5": "1.5rem",
    "--space-6": "2rem",
    "--radius-sm": "6px",
    "--radius-md": "10px",
    "--radius-lg": "14px",
    "--radius-pill": "999px",
    "--text-xs": "0.75rem",
    "--text-sm": "0.875rem",
    "--text-base": "1rem",
    "--text-lg": "1.125rem",
    "--text-xl": "1.375rem",
    "--leading-tight": "1.25",
    "--leading": "1.5",
    "--weight-medium": "500",
    "--weight-bold": "650",
    "--font-sans": "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif",
    "--focus-ring-width": "2px",
    "--focus-ring-offset": "2px",
    "--focus-ring": "var(--focus-ring-width) solid var(--color-focus)",
    "--page-max": "76rem",
    "--bar-height": "3.5rem",
    # --sidebar stays an app token: ocr has a sidebar inside the content width.
    "--sidebar": "22rem",
}


def _root_tokens() -> dict[str, str]:
    css = re.sub(r"/\*.*?\*/", "", STYLE.read_text(), flags=re.DOTALL)
    block = re.search(r":root\s*\{(.*?)\}", css, re.DOTALL)
    assert block, "style.css has no :root block"
    pairs = re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", block.group(1))
    return {name: value.strip() for name, value in pairs}


def _assert_colour(tokens: dict[str, str], name: str, light: str, dark: str) -> None:
    assert tokens.get(name) == f"light-dark({light}, {dark})", name


@pytest.mark.parametrize(("name", "pair"), sorted(SHARED_COLOURS.items()))
def test_shared_colour_tokens_match_the_spec_table(name, pair):
    _assert_colour(_root_tokens(), name, *pair)


@pytest.mark.parametrize(("name", "pair"), sorted(OCR_ACCENT_COLOURS.items()))
def test_accent_tokens_match_the_ocr_column_of_the_spec(name, pair):
    _assert_colour(_root_tokens(), name, *pair)


@pytest.mark.parametrize(("name", "value"), sorted(SCALAR_TOKENS.items()))
def test_scalar_tokens_match_the_spec(name, value):
    assert _root_tokens().get(name) == value, name


def test_the_colour_token_inventory_is_the_spec_plus_the_app_highlights():
    colours = {name for name in _root_tokens() if name.startswith("--color-")}
    assert colours == set(SHARED_COLOURS) | set(OCR_ACCENT_COLOURS) | APP_COLOURS


def test_every_colour_is_a_light_dark_pair_and_the_root_follows_the_system_scheme():
    tokens = _root_tokens()
    for name in sorted(set(SHARED_COLOURS) | set(OCR_ACCENT_COLOURS) | APP_COLOURS):
        assert re.fullmatch(r"light-dark\(.+, .+\)", tokens[name]), name
    css = re.sub(r"/\*.*?\*/", "", STYLE.read_text(), flags=re.DOTALL)
    block = re.search(r":root\s*\{(.*?)\}", css, re.DOTALL).group(1)
    assert "color-scheme: light dark;" in block


def test_the_colour_check_catches_a_drifted_token_and_passes_the_spec_value():
    # The real bad output: ocr shipped #2d1717 for the dark banner before this audit.
    drifted = {"--color-danger-subtle": "light-dark(#fdecea, #2d1717)"}
    with pytest.raises(AssertionError):
        _assert_colour(drifted, "--color-danger-subtle", *SHARED_COLOURS["--color-danger-subtle"])
    missing = {}
    with pytest.raises(AssertionError):
        _assert_colour(missing, "--color-danger-subtle", *SHARED_COLOURS["--color-danger-subtle"])
    for name, (light, dark) in SHARED_COLOURS.items():
        _assert_colour({name: f"light-dark({light}, {dark})"}, name, light, dark)
