"""Widget content extraction and rendering.

This module extracts renderable content (HTML, SVG, images) from Lean widget props.
It uses a tiered approach that prioritizes fast content extraction over JS execution.

Architecture:
    Tier 0: Type-aware extraction - recognize widget type, extract pre-rendered content
    Tier 1: Props pattern matching - recursively find html/svg/images in props
    Tier 2: JS execution (optional) - full React SSR for dynamic widgets

Most ProofWidgets (like #html, #svg, #png) already contain rendered content,
so Tier 0/1 handle the common cases without any JavaScript execution.

Usage:
    from leanclient.widget_render import extract_widget_content, WidgetContent

    # Extract content from widget props
    content = extract_widget_content(widget)
    if content.html:
        print(content.html)
    for mime, data in content.images:
        save_image(mime, data)
"""

from __future__ import annotations

import base64
import html
import json
import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class WidgetContent:
    """Extracted content from a widget.

    Attributes:
        widget_type: The widget type name (e.g., 'ProofWidgets.HtmlDisplay')
        html: Extracted HTML content, if any
        svg: Extracted SVG content, if any
        text: Plain text fallback
        images: List of (mime_type, bytes) tuples for embedded images
        raw_props: Original widget props for further processing
        extraction_tier: Which tier extracted the content (0, 1, or 2)
    """

    widget_type: str | None = None
    html: str | None = None
    svg: str | None = None
    text: str | None = None
    images: list[tuple[str, bytes]] = field(default_factory=list)
    raw_props: dict = field(default_factory=dict)
    extraction_tier: int = -1

    @property
    def has_content(self) -> bool:
        """Check if any content was extracted."""
        return bool(self.html or self.svg or self.text or self.images)

    def to_html(self) -> str:
        """Convert all content to a single HTML string."""
        parts = []

        if self.html:
            parts.append(self.html)

        if self.svg:
            parts.append(self.svg)

        for mime, data in self.images:
            b64 = base64.b64encode(data).decode("ascii")
            parts.append(f'<img src="data:{mime};base64,{b64}" />')

        if not parts and self.text:
            parts.append(f"<pre>{html.escape(self.text)}</pre>")

        if not parts:
            return "<!-- No renderable content -->"

        return "\n".join(parts)


# =============================================================================
# Tier 0: Type-Aware Content Extraction
# =============================================================================

# Known widget types and their content extraction paths
WIDGET_EXTRACTORS: dict[str, list[str]] = {
    # ProofWidgets HTML/SVG display
    "ProofWidgets.HtmlDisplay": ["html"],
    "ProofWidgets.SvgDisplay": ["svg"],
    "ProofWidgets.HtmlDisplayPanel": ["html"],
    # Image widgets
    "ProofWidgets.PngDisplay": ["png", "image", "base64"],
    # Penrose diagrams
    "ProofWidgets.PenroseDisplay": ["svg", "rendered"],
    # Goal display
    "Lean.Widget.InteractiveGoal": ["goal", "text"],
    "Lean.Widget.InteractiveGoals": ["goals"],
    # Generic content
    "html": ["html", "content"],
    "svg": ["svg", "content"],
}


def _extract_by_path(props: dict, path: str) -> Any:
    """Extract value from props by dot-separated path."""
    current = props
    for key in path.split("."):
        if isinstance(current, dict) and key in current:
            current = current[key]
        else:
            return None
    return current


def _tier0_extract(widget: dict) -> WidgetContent | None:
    """Tier 0: Extract content using known widget type patterns.

    This is the fastest path - if we recognize the widget type,
    we know exactly where to find the content.
    """
    widget_type = widget.get("name?") or widget.get("name")
    props = widget.get("props", {})

    if not widget_type:
        return None

    # Check if we have an extractor for this type
    paths = WIDGET_EXTRACTORS.get(widget_type)
    if not paths:
        # Try partial matches
        for known_type, known_paths in WIDGET_EXTRACTORS.items():
            if known_type in widget_type:
                paths = known_paths
                break

    if not paths:
        return None

    content = WidgetContent(
        widget_type=widget_type, raw_props=props, extraction_tier=0
    )

    for path in paths:
        value = _extract_by_path(props, path)
        if value is None:
            continue

        if isinstance(value, str):
            # Determine content type
            stripped = value.strip()
            if stripped.startswith("<svg") or stripped.startswith("<?xml"):
                content.svg = value
            elif stripped.startswith("<") and ">" in stripped:
                content.html = value
            elif path in ("png", "image", "base64"):
                # Base64 image data
                try:
                    img_bytes = base64.b64decode(value)
                    content.images.append(("image/png", img_bytes))
                except Exception:
                    pass
            else:
                content.text = value
        elif isinstance(value, dict):
            # Could be structured HTML/SVG
            if "tag" in value or "children" in value:
                # Tagged text format - convert to HTML
                content.html = _tagged_text_to_html(value)

    return content if content.has_content else None


def _tagged_text_to_html(tt: Any) -> str:
    """Convert Lean's TaggedText format to HTML."""
    if tt is None:
        return ""
    if isinstance(tt, str):
        return html.escape(tt)
    if isinstance(tt, list):
        return "".join(_tagged_text_to_html(item) for item in tt)
    if isinstance(tt, dict):
        # Handle different TaggedText variants
        if "text" in tt:
            return html.escape(str(tt["text"]))
        if "append" in tt:
            return "".join(_tagged_text_to_html(item) for item in tt["append"])
        if "tag" in tt:
            tag_content = tt["tag"]
            if isinstance(tag_content, list):
                return "".join(_tagged_text_to_html(item) for item in tag_content)
            return _tagged_text_to_html(tag_content)
        # Style info
        info = tt.get("info")
        content = _tagged_text_to_html(tt.get("content", tt.get("children", "")))
        if info and isinstance(info, dict):
            cls = info.get("cls", "")
            if cls:
                return f'<span class="{html.escape(cls)}">{content}</span>'
        return content
    return ""


# =============================================================================
# Tier 1: Props Pattern Matching
# =============================================================================


def _tier1_extract(widget: dict) -> WidgetContent:
    """Tier 1: Recursively search props for extractable content.

    This handles widgets where we don't recognize the type but
    the props contain html/svg/images somewhere in the structure.
    """
    widget_type = widget.get("name?") or widget.get("name")
    props = widget.get("props", {})

    content = WidgetContent(
        widget_type=widget_type, raw_props=props, extraction_tier=1
    )

    # Recursively search for content
    _search_props(props, content, depth=0)

    return content


def _search_props(obj: Any, content: WidgetContent, depth: int) -> None:
    """Recursively search an object for extractable content."""
    if depth > 20:  # Prevent infinite recursion
        return

    if isinstance(obj, str):
        stripped = obj.strip()
        # Check for HTML/SVG
        if len(stripped) > 10:
            if stripped.startswith("<svg") or "<svg " in stripped[:100]:
                if not content.svg:
                    content.svg = obj
            elif stripped.startswith("<") and ">" in stripped[:100]:
                # Looks like HTML
                if not content.html:
                    content.html = obj
        # Check for base64 image
        if len(obj) > 100 and not any(c in obj for c in " \n\t<>"):
            try:
                decoded = base64.b64decode(obj)
                # Check for image magic bytes
                if decoded[:4] == b"\x89PNG" or decoded[:2] == b"\xff\xd8":
                    mime = "image/png" if decoded[:4] == b"\x89PNG" else "image/jpeg"
                    content.images.append((mime, decoded))
            except Exception:
                pass

    elif isinstance(obj, dict):
        # Check for known content keys
        for key in ("html", "svg", "content", "rendered", "output"):
            if key in obj and isinstance(obj[key], str):
                val = obj[key].strip()
                if key == "svg" or val.startswith("<svg"):
                    if not content.svg:
                        content.svg = obj[key]
                elif val.startswith("<"):
                    if not content.html:
                        content.html = obj[key]

        # Check for base64 image structure
        if "base64" in obj and "mimeType" in obj:
            try:
                data = base64.b64decode(obj["base64"])
                content.images.append((obj["mimeType"], data))
            except Exception:
                pass

        # Check for data URL image
        if "image" in obj and isinstance(obj["image"], str):
            img = obj["image"]
            if img.startswith("data:"):
                if "," in img:
                    header, b64_data = img.split(",", 1)
                    mime = "image/png"
                    if ":" in header and ";" in header:
                        mime = header.split(":")[1].split(";")[0]
                    try:
                        content.images.append((mime, base64.b64decode(b64_data)))
                    except Exception:
                        pass

        # Recurse into values
        for v in obj.values():
            _search_props(v, content, depth + 1)

    elif isinstance(obj, list):
        for item in obj:
            _search_props(item, content, depth + 1)


# =============================================================================
# Tier 2: JavaScript Execution (Optional)
# =============================================================================

_quickjs_available: bool | None = None
_deno_available: bool | None = None


def _check_quickjs() -> bool:
    """Check if quickjs Python package is available."""
    global _quickjs_available
    if _quickjs_available is not None:
        return _quickjs_available
    try:
        import quickjs  # noqa: F401

        _quickjs_available = True
    except ImportError:
        _quickjs_available = False
    return _quickjs_available


def _check_deno() -> bool:
    """Check if deno is available on the system."""
    global _deno_available
    if _deno_available is not None:
        return _deno_available
    _deno_available = shutil.which("deno") is not None
    return _deno_available


def js_execution_available() -> bool:
    """Check if JS execution is available for Tier 2."""
    return _check_quickjs() or _check_deno()


# Minimal React-like SSR shim
REACT_SSR_SHIM = """
const React = {
    createElement: (type, props, ...children) => {
        if (typeof type === 'function') {
            return type({...props, children: children.flat()});
        }
        return {type, props: {...props, children: children.flat()}};
    },
    Fragment: Symbol('Fragment')
};

function renderToString(el) {
    if (el == null) return '';
    if (typeof el === 'string' || typeof el === 'number') return String(el);
    if (Array.isArray(el)) return el.map(renderToString).join('');
    if (typeof el !== 'object') return '';

    const {type, props = {}} = el;
    const {children = [], ...attrs} = props;

    if (type === React.Fragment) {
        return [].concat(children).map(renderToString).join('');
    }

    const attrStr = Object.entries(attrs)
        .filter(([k, v]) => v != null && v !== false && k !== 'key')
        .map(([k, v]) => {
            const name = k === 'className' ? 'class' : k;
            if (v === true) return name;
            if (k === 'style' && typeof v === 'object') {
                const s = Object.entries(v)
                    .map(([p, s]) => `${p.replace(/[A-Z]/g, m => '-'+m.toLowerCase())}:${s}`)
                    .join(';');
                return `style="${s}"`;
            }
            return `${name}="${String(v).replace(/"/g, '&quot;')}"`;
        })
        .join(' ');

    const tag = typeof type === 'string' ? type : 'div';
    const childHtml = [].concat(children).map(renderToString).join('');

    const selfClosing = ['img','br','hr','input','meta','link','area','base','col'];
    if (selfClosing.includes(tag) && !childHtml) {
        return `<${tag}${attrStr ? ' ' + attrStr : ''} />`;
    }
    return `<${tag}${attrStr ? ' ' + attrStr : ''}>${childHtml}</${tag}>`;
}
"""


def _tier2_extract(widget: dict, js_source: str | None = None) -> WidgetContent | None:
    """Tier 2: Execute JavaScript to render the widget.

    This is the slow path - only used when Tier 0/1 fail and
    we have actual JS source to execute.
    """
    if not js_source:
        return None

    widget_type = widget.get("name?") or widget.get("name")
    props = widget.get("props", {})

    content = WidgetContent(
        widget_type=widget_type, raw_props=props, extraction_tier=2
    )

    props_json = json.dumps(props)

    if _check_quickjs():
        try:
            import quickjs

            ctx = quickjs.Context()
            ctx.eval(REACT_SSR_SHIM)

            # Try to render the component
            result = ctx.eval(
                f"""
                (function() {{
                    const __props = {props_json};
                    {js_source}
                    if (typeof __default !== 'undefined') return renderToString(__default(__props));
                    if (typeof Widget !== 'undefined') return renderToString(Widget(__props));
                    if (typeof render !== 'undefined') return renderToString(render(__props));
                    return '';
                }})()
                """
            )
            if result:
                content.html = str(result)
                return content
        except Exception as e:
            logger.debug(f"quickjs execution failed: {e}")

    if _check_deno():
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".js", delete=False
            ) as f:
                f.write(REACT_SSR_SHIM)
                f.write(f"\nconst __props = {props_json};\n")
                f.write(js_source)
                f.write(
                    """
                    let result = '';
                    if (typeof __default !== 'undefined') result = renderToString(__default(__props));
                    else if (typeof Widget !== 'undefined') result = renderToString(Widget(__props));
                    else if (typeof render !== 'undefined') result = renderToString(render(__props));
                    console.log(result);
                    """
                )
                f.flush()

                result = subprocess.run(
                    ["deno", "run", f.name],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                Path(f.name).unlink(missing_ok=True)

                if result.returncode == 0 and result.stdout.strip():
                    content.html = result.stdout.strip()
                    return content
        except Exception as e:
            logger.debug(f"deno execution failed: {e}")

    return None


# =============================================================================
# Main API
# =============================================================================


def extract_widget_content(
    widget: dict,
    js_source: str | None = None,
    enable_js_execution: bool = False,
) -> WidgetContent:
    """Extract renderable content from a widget.

    Uses a tiered approach:
    1. Tier 0: Type-aware extraction (instant)
    2. Tier 1: Props pattern matching (fast)
    3. Tier 2: JS execution (slow, requires opt-in)

    Args:
        widget: Widget dict from get_widgets() or get_interactive_diagnostics()
        js_source: Optional JS source from get_widget_source() for Tier 2
        enable_js_execution: Enable Tier 2 JS execution (default False)

    Returns:
        WidgetContent with extracted html/svg/images/text
    """
    # Tier 0: Type-aware extraction
    content = _tier0_extract(widget)
    if content and content.has_content:
        return content

    # Tier 1: Props pattern matching
    content = _tier1_extract(widget)
    if content.has_content:
        return content

    # Tier 2: JS execution (if enabled and available)
    if enable_js_execution and js_source:
        js_content = _tier2_extract(widget, js_source)
        if js_content and js_content.has_content:
            return js_content

    # Return empty content with metadata
    return WidgetContent(
        widget_type=widget.get("name?") or widget.get("name"),
        raw_props=widget.get("props", {}),
        extraction_tier=-1,
    )


def extract_images(widget: dict) -> list[tuple[str, bytes]]:
    """Convenience function to extract just images from a widget.

    Args:
        widget: Widget dict

    Returns:
        List of (mime_type, bytes) tuples
    """
    content = extract_widget_content(widget)
    return content.images


def widget_to_html(
    widget: dict,
    js_source: str | None = None,
    enable_js_execution: bool = False,
) -> str:
    """Convert a widget to HTML string.

    Args:
        widget: Widget dict
        js_source: Optional JS source for dynamic rendering
        enable_js_execution: Enable JS execution for dynamic widgets

    Returns:
        HTML string representation
    """
    content = extract_widget_content(widget, js_source, enable_js_execution)
    return content.to_html()


# Legacy compatibility - keep extract_images_from_widget_props
def extract_images_from_widget_props(props: dict) -> list[tuple[str, bytes]]:
    """Extract base64 images from widget props (legacy API).

    Args:
        props: Widget props dictionary

    Returns:
        List of (mime_type, bytes) tuples
    """
    return extract_images({"props": props})


# Renderer classes for backwards compatibility
class WidgetRenderer:
    """Context manager for widget rendering (compatibility API)."""

    def __enter__(self) -> "WidgetRenderer":
        return self

    def __exit__(self, *args) -> None:
        pass

    def render(self, js_source: str, props: dict) -> str:
        """Render a widget to HTML."""
        widget = {"props": props}
        content = extract_widget_content(
            widget, js_source=js_source, enable_js_execution=True
        )
        return content.to_html()

    @property
    def backend_name(self) -> str:
        """Get active JS backend name."""
        if _check_quickjs():
            return "quickjs"
        if _check_deno():
            return "deno"
        return "none"


def render_widget(js_source: str, props: dict) -> str:
    """One-shot widget rendering (compatibility API)."""
    with WidgetRenderer() as renderer:
        return renderer.render(js_source, props)


def is_available() -> bool:
    """Check if any rendering backend is available."""
    return True  # Tier 0/1 always work; Tier 2 is optional


def get_backend() -> str:
    """Get active rendering backend."""
    if _check_quickjs():
        return "quickjs"
    if _check_deno():
        return "deno"
    return "extraction"  # Tier 0/1 always available
