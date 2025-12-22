"""Tests for widget_render module - tiered content extraction."""

import base64

import pytest

from leanclient.widget_render import (
    WidgetContent,
    extract_widget_content,
    extract_images,
    widget_to_html,
    extract_images_from_widget_props,
    _tier0_extract,
    _tier1_extract,
    _tagged_text_to_html,
    is_available,
    get_backend,
    js_execution_available,
    _check_quickjs,
    _check_deno,
    WIDGET_EXTRACTORS,
)


# =============================================================================
# WidgetContent Tests
# =============================================================================


def test_widget_content_has_content_empty():
    """Empty WidgetContent should report no content."""
    content = WidgetContent()
    assert not content.has_content


def test_widget_content_has_content_with_html():
    """WidgetContent with HTML should report has_content."""
    content = WidgetContent(html="<div>test</div>")
    assert content.has_content


def test_widget_content_has_content_with_svg():
    """WidgetContent with SVG should report has_content."""
    content = WidgetContent(svg="<svg></svg>")
    assert content.has_content


def test_widget_content_has_content_with_images():
    """WidgetContent with images should report has_content."""
    content = WidgetContent(images=[("image/png", b"data")])
    assert content.has_content


def test_widget_content_to_html_empty():
    """Empty content should produce comment."""
    content = WidgetContent()
    assert "No renderable content" in content.to_html()


def test_widget_content_to_html_with_html():
    """HTML content should be returned as-is."""
    content = WidgetContent(html="<div>Hello</div>")
    assert content.to_html() == "<div>Hello</div>"


def test_widget_content_to_html_with_svg():
    """SVG content should be returned as-is."""
    content = WidgetContent(svg="<svg><circle/></svg>")
    assert content.to_html() == "<svg><circle/></svg>"


def test_widget_content_to_html_with_images():
    """Images should be converted to data URLs."""
    png_data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 10
    content = WidgetContent(images=[("image/png", png_data)])
    html = content.to_html()
    assert "data:image/png;base64," in html
    assert "<img" in html


def test_widget_content_to_html_text_fallback():
    """Text should be wrapped in pre with escaping."""
    content = WidgetContent(text="<script>alert('xss')</script>")
    html = content.to_html()
    assert "<pre>" in html
    assert "&lt;script&gt;" in html


# =============================================================================
# Tier 0: Type-Aware Extraction Tests
# =============================================================================


def test_tier0_html_display():
    """Tier 0 should extract HTML from HtmlDisplay widget."""
    widget = {
        "name?": "ProofWidgets.HtmlDisplay",
        "props": {"html": "<div>Test Widget</div>"},
    }
    content = _tier0_extract(widget)
    assert content is not None
    assert content.html == "<div>Test Widget</div>"
    assert content.extraction_tier == 0


def test_tier0_svg_display():
    """Tier 0 should extract SVG from SvgDisplay widget."""
    widget = {
        "name?": "ProofWidgets.SvgDisplay",
        "props": {"svg": "<svg><rect/></svg>"},
    }
    content = _tier0_extract(widget)
    assert content is not None
    assert content.svg == "<svg><rect/></svg>"


def test_tier0_png_display():
    """Tier 0 should extract images from PngDisplay widget."""
    png_data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 10
    widget = {
        "name?": "ProofWidgets.PngDisplay",
        "props": {"png": base64.b64encode(png_data).decode()},
    }
    content = _tier0_extract(widget)
    assert content is not None
    assert len(content.images) == 1
    assert content.images[0][1] == png_data


def test_tier0_partial_type_match():
    """Tier 0 should match partial widget type names."""
    widget = {
        "name?": "MyApp.ProofWidgets.HtmlDisplay.Custom",
        "props": {"html": "<p>Custom</p>"},
    }
    content = _tier0_extract(widget)
    assert content is not None
    assert content.html == "<p>Custom</p>"


def test_tier0_unknown_type_returns_none():
    """Tier 0 should return None for unknown widget types."""
    widget = {
        "name?": "UnknownWidget",
        "props": {"data": "something"},
    }
    content = _tier0_extract(widget)
    assert content is None


def test_tier0_no_type_returns_none():
    """Tier 0 should return None when no type is present."""
    widget = {"props": {"html": "<div>test</div>"}}
    content = _tier0_extract(widget)
    assert content is None


# =============================================================================
# TaggedText Conversion Tests
# =============================================================================


def test_tagged_text_string():
    """Simple string should be escaped."""
    assert _tagged_text_to_html("hello") == "hello"
    assert _tagged_text_to_html("<script>") == "&lt;script&gt;"


def test_tagged_text_list():
    """List should concatenate items."""
    assert _tagged_text_to_html(["a", "b", "c"]) == "abc"


def test_tagged_text_dict_with_text():
    """Dict with text key should extract text."""
    assert _tagged_text_to_html({"text": "hello"}) == "hello"


def test_tagged_text_dict_with_append():
    """Dict with append should concatenate items."""
    result = _tagged_text_to_html({"append": ["a", {"text": "b"}]})
    assert result == "ab"


def test_tagged_text_dict_with_tag():
    """Dict with tag should recurse."""
    result = _tagged_text_to_html({"tag": [{"text": "x"}, {"text": "y"}]})
    assert result == "xy"


def test_tagged_text_with_style_info():
    """Dict with info.cls should add span with class."""
    result = _tagged_text_to_html({
        "info": {"cls": "highlight"},
        "content": "styled"
    })
    assert 'class="highlight"' in result
    assert "styled" in result


# =============================================================================
# Tier 1: Props Pattern Matching Tests
# =============================================================================


def test_tier1_finds_nested_html():
    """Tier 1 should find HTML nested in props."""
    widget = {
        "name?": "CustomWidget",
        "props": {
            "config": {
                "display": {
                    "html": "<div>Nested</div>"
                }
            }
        },
    }
    content = _tier1_extract(widget)
    assert content.html == "<div>Nested</div>"
    assert content.extraction_tier == 1


def test_tier1_finds_nested_svg():
    """Tier 1 should find SVG nested in props."""
    widget = {
        "props": {
            "chart": {
                "svg": "<svg><path/></svg>"
            }
        },
    }
    content = _tier1_extract(widget)
    assert content.svg == "<svg><path/></svg>"


def test_tier1_finds_base64_image_structure():
    """Tier 1 should find base64/mimeType image structures."""
    png_data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 10
    widget = {
        "props": {
            "image": {
                "base64": base64.b64encode(png_data).decode(),
                "mimeType": "image/png"
            }
        },
    }
    content = _tier1_extract(widget)
    assert len(content.images) == 1
    assert content.images[0] == ("image/png", png_data)


def test_tier1_finds_data_url_image():
    """Tier 1 should find data URL images."""
    png_data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 10
    b64 = base64.b64encode(png_data).decode()
    widget = {
        "props": {
            "image": f"data:image/png;base64,{b64}"
        },
    }
    content = _tier1_extract(widget)
    assert len(content.images) == 1
    assert content.images[0][0] == "image/png"


def test_tier1_detects_svg_in_string():
    """Tier 1 should detect SVG content in strings."""
    widget = {
        "props": {
            "data": "<svg viewBox='0 0 100 100'><circle/></svg>"
        },
    }
    content = _tier1_extract(widget)
    assert content.svg is not None
    assert "viewBox" in content.svg


def test_tier1_respects_depth_limit():
    """Tier 1 should not crash on deeply nested structures."""
    # Create deeply nested structure
    props = {}
    current = props
    for _ in range(30):
        current["nested"] = {}
        current = current["nested"]
    current["html"] = "<div>Deep</div>"

    widget = {"props": props}
    content = _tier1_extract(widget)
    # Should not crash, may or may not find the content
    assert isinstance(content, WidgetContent)


# =============================================================================
# Main API Tests
# =============================================================================


def test_extract_widget_content_uses_tier0_first():
    """extract_widget_content should prefer Tier 0."""
    widget = {
        "name?": "ProofWidgets.HtmlDisplay",
        "props": {"html": "<div>Tier0</div>"},
    }
    content = extract_widget_content(widget)
    assert content.extraction_tier == 0
    assert content.html == "<div>Tier0</div>"


def test_extract_widget_content_falls_back_to_tier1():
    """extract_widget_content should fall back to Tier 1."""
    widget = {
        "name?": "UnknownWidget",
        "props": {"nested": {"html": "<div>Tier1</div>"}},
    }
    content = extract_widget_content(widget)
    assert content.extraction_tier == 1
    assert content.html == "<div>Tier1</div>"


def test_extract_widget_content_returns_empty_on_failure():
    """extract_widget_content should return empty content on failure."""
    widget = {
        "name?": "EmptyWidget",
        "props": {"data": 42},
    }
    content = extract_widget_content(widget)
    assert not content.has_content
    assert content.extraction_tier == -1


def test_extract_images_convenience():
    """extract_images should return just the images list."""
    png_data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 10
    widget = {
        "props": {
            "image": {
                "base64": base64.b64encode(png_data).decode(),
                "mimeType": "image/png"
            }
        },
    }
    images = extract_images(widget)
    assert len(images) == 1
    assert images[0] == ("image/png", png_data)


def test_widget_to_html():
    """widget_to_html should return HTML string."""
    widget = {
        "name?": "ProofWidgets.HtmlDisplay",
        "props": {"html": "<p>Hello</p>"},
    }
    html = widget_to_html(widget)
    assert html == "<p>Hello</p>"


def test_extract_images_from_widget_props_legacy():
    """Legacy API should still work."""
    png_data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 10
    props = {
        "image": {
            "base64": base64.b64encode(png_data).decode(),
            "mimeType": "image/png"
        }
    }
    images = extract_images_from_widget_props(props)
    assert len(images) == 1


# =============================================================================
# Availability and Backend Tests
# =============================================================================


def test_is_available_always_true():
    """is_available should always be True (Tier 0/1 always work)."""
    assert is_available() is True


def test_get_backend_returns_valid():
    """get_backend should return a valid backend name."""
    backend = get_backend()
    assert backend in ("quickjs", "deno", "extraction")


def test_js_execution_available_returns_bool():
    """js_execution_available should return a boolean."""
    result = js_execution_available()
    assert isinstance(result, bool)


def test_check_quickjs_returns_bool():
    """_check_quickjs should return a boolean."""
    result = _check_quickjs()
    assert isinstance(result, bool)


def test_check_deno_returns_bool():
    """_check_deno should return a boolean."""
    result = _check_deno()
    assert isinstance(result, bool)


# =============================================================================
# JS Execution Tests (Tier 2)
# =============================================================================


@pytest.mark.skipif(not js_execution_available(), reason="No JS backend available")
def test_tier2_js_execution():
    """Tier 2 should execute JS when enabled."""
    from leanclient.widget_render import WidgetRenderer

    js_source = """
    const __default = (props) => React.createElement('div', null, props.text);
    """

    with WidgetRenderer() as renderer:
        html = renderer.render(js_source, {"text": "Dynamic"})
        assert "Dynamic" in html


@pytest.mark.skipif(not _check_deno(), reason="deno not installed")
def test_deno_backend():
    """Test deno backend specifically."""
    from leanclient.widget_render import WidgetRenderer

    js_source = """
    const __default = (props) => React.createElement('span', null, 'Deno');
    """

    with WidgetRenderer() as renderer:
        html = renderer.render(js_source, {})
        assert "Deno" in html or "span" in html


# =============================================================================
# Integration Tests
# =============================================================================


def test_full_widget_extraction_pipeline():
    """Test the complete extraction pipeline with a realistic widget."""
    # Simulated #html widget
    widget = {
        "id": "widget-abc123",
        "javascriptHash": "hash123",
        "name?": "ProofWidgets.HtmlDisplay",
        "range": {
            "start": {"line": 10, "character": 0},
            "end": {"line": 10, "character": 20},
        },
        "props": {
            "html": "<div class='proof-widget'><p>Proof complete!</p></div>"
        },
    }

    content = extract_widget_content(widget)

    assert content.widget_type == "ProofWidgets.HtmlDisplay"
    assert content.extraction_tier == 0
    assert content.has_content
    assert "Proof complete!" in content.html
    assert "proof-widget" in content.to_html()


def test_mixed_content_extraction():
    """Test extracting multiple content types from one widget."""
    png_data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 10
    widget = {
        "props": {
            "html": "<div>Description</div>",
            "chart": {"svg": "<svg><rect/></svg>"},
            "thumbnail": {
                "base64": base64.b64encode(png_data).decode(),
                "mimeType": "image/png",
            },
        },
    }

    content = extract_widget_content(widget)

    # Should find HTML via Tier 1
    assert content.html is not None
    # May also find other content
    combined = content.to_html()
    assert "Description" in combined
