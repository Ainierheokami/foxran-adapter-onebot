"""The WebUI bundle's Tailwind utilities are prefixed, so they never override the WebUI's own.

An unprefixed `.flex{display:flex}` injected after the WebUI's CSS beat its `md:hidden`
and showed the mobile header on desktop.
"""

import re
from pathlib import Path

import pytest

BUNDLE = Path(__file__).resolve().parents[1] / "index.umd.js"
PREFIX = "ob"


@pytest.mark.skipif(not BUNDLE.exists(), reason="index.umd.js is not built")
def test_every_utility_selector_carries_the_prefix():
    css = BUNDLE.read_text(encoding="utf-8")
    utilities = css[css.index("@layer utilities{"):]
    selectors = re.findall(r"(?:^|[{}])\s*(\.[^{},@]+)\{", utilities)

    assert selectors
    assert [s for s in selectors if not s.startswith(f".{PREFIX}\\\\:")] == []


@pytest.mark.skipif(not BUNDLE.exists(), reason="index.umd.js is not built")
def test_theme_variables_do_not_shadow_the_webui():
    css = BUNDLE.read_text(encoding="utf-8")

    assert not re.search(r"--(?:color-[a-z-]+|spacing):", css)
