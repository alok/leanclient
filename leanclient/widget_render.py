"""Widget content extraction from Lean widget props.

Tiered approach:
  - Tier 0: Match widget type, extract from known prop keys
  - Tier 1: Recursively search props for html/svg/base64 patterns
  - Tier 2: JS execution via quickjs/deno (optional)
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
    """Extracted content from a widget."""

    widget_type: str | None = None
    html: str | None = None
    svg: str | None = None
    text: str | None = None
    images: list[tuple[str, bytes]] = field(default_factory=list)
    raw_props: dict = field(default_factory=dict)
    extraction_tier: int = -1

    @property
    def has_content(self) -> bool:
        return bool(self.html or self.svg or self.text or self.images)

    def to_html(self) -> str:
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
        return "\n".join(parts) if parts else "<!-- No renderable content -->"


# Widget type -> prop keys to check
WIDGET_EXTRACTORS: dict[str, list[str]] = {
    "ProofWidgets.HtmlDisplay": ["html"],
    "ProofWidgets.SvgDisplay": ["svg"],
    "ProofWidgets.HtmlDisplayPanel": ["html"],
    "ProofWidgets.PngDisplay": ["png", "image", "base64"],
    "ProofWidgets.PenroseDisplay": ["svg", "rendered"],
    "Lean.Widget.InteractiveGoal": ["goal", "text"],
    "Lean.Widget.InteractiveGoals": ["goals"],
    "html": ["html", "content"],
    "svg": ["svg", "content"],
}


def _extract_by_path(props: dict, path: str) -> Any:
    current = props
    for key in path.split("."):
        if isinstance(current, dict) and key in current:
            current = current[key]
        else:
            return None
    return current


def _tier0_extract(widget: dict) -> WidgetContent | None:
    """Extract content using known widget type patterns."""
    widget_type = widget.get("name?") or widget.get("name")
    props = widget.get("props", {})

    if not widget_type:
        return None

    paths = WIDGET_EXTRACTORS.get(widget_type)
    if not paths:
        for known_type, known_paths in WIDGET_EXTRACTORS.items():
            if known_type in widget_type:
                paths = known_paths
                break

    if not paths:
        return None

    content = WidgetContent(widget_type=widget_type, raw_props=props, extraction_tier=0)

    for path in paths:
        value = _extract_by_path(props, path)
        if value is None:
            continue

        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("<svg") or stripped.startswith("<?xml"):
                content.svg = value
            elif stripped.startswith("<") and ">" in stripped:
                content.html = value
            elif path in ("png", "image", "base64"):
                try:
                    content.images.append(("image/png", base64.b64decode(value)))
                except Exception:
                    pass
            else:
                content.text = value
        elif isinstance(value, dict) and ("tag" in value or "children" in value):
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
        if "text" in tt:
            return html.escape(str(tt["text"]))
        if "append" in tt:
            return "".join(_tagged_text_to_html(item) for item in tt["append"])
        if "tag" in tt:
            tag_content = tt["tag"]
            if isinstance(tag_content, list):
                return "".join(_tagged_text_to_html(item) for item in tag_content)
            return _tagged_text_to_html(tag_content)
        info = tt.get("info")
        content = _tagged_text_to_html(tt.get("content", tt.get("children", "")))
        if info and isinstance(info, dict):
            cls = info.get("cls", "")
            if cls:
                return f'<span class="{html.escape(cls)}">{content}</span>'
        return content
    return ""


def _tier1_extract(widget: dict) -> WidgetContent:
    """Recursively search props for extractable content."""
    widget_type = widget.get("name?") or widget.get("name")
    props = widget.get("props", {})
    content = WidgetContent(widget_type=widget_type, raw_props=props, extraction_tier=1)
    _search_props(props, content, depth=0)
    return content


def _search_props(obj: Any, content: WidgetContent, depth: int) -> None:
    if depth > 20:
        return

    if isinstance(obj, str):
        stripped = obj.strip()
        if len(stripped) > 10:
            if stripped.startswith("<svg") or "<svg " in stripped[:100]:
                if not content.svg:
                    content.svg = obj
            elif stripped.startswith("<") and ">" in stripped[:100]:
                if not content.html:
                    content.html = obj
        if len(obj) > 100 and not any(c in obj for c in " \n\t<>"):
            try:
                decoded = base64.b64decode(obj)
                if decoded[:4] == b"\x89PNG" or decoded[:2] == b"\xff\xd8":
                    mime = "image/png" if decoded[:4] == b"\x89PNG" else "image/jpeg"
                    content.images.append((mime, decoded))
            except Exception:
                pass

    elif isinstance(obj, dict):
        for key in ("html", "svg", "content", "rendered", "output"):
            if key in obj and isinstance(obj[key], str):
                val = obj[key].strip()
                if key == "svg" or val.startswith("<svg"):
                    if not content.svg:
                        content.svg = obj[key]
                elif val.startswith("<"):
                    if not content.html:
                        content.html = obj[key]

        if "base64" in obj and "mimeType" in obj:
            try:
                content.images.append((obj["mimeType"], base64.b64decode(obj["base64"])))
            except Exception:
                pass

        if "image" in obj and isinstance(obj["image"], str):
            img = obj["image"]
            if img.startswith("data:") and "," in img:
                header, b64_data = img.split(",", 1)
                mime = "image/png"
                if ":" in header and ";" in header:
                    mime = header.split(":")[1].split(";")[0]
                try:
                    content.images.append((mime, base64.b64decode(b64_data)))
                except Exception:
                    pass

        for v in obj.values():
            _search_props(v, content, depth + 1)

    elif isinstance(obj, list):
        for item in obj:
            _search_props(item, content, depth + 1)


# Tier 2: JS execution

_quickjs_available: bool | None = None
_deno_available: bool | None = None


def _check_quickjs() -> bool:
    global _quickjs_available
    if _quickjs_available is None:
        try:
            import quickjs  # noqa: F401
            _quickjs_available = True
        except ImportError:
            _quickjs_available = False
    return _quickjs_available


def _check_deno() -> bool:
    global _deno_available
    if _deno_available is None:
        _deno_available = shutil.which("deno") is not None
    return _deno_available


def js_execution_available() -> bool:
    return _check_quickjs() or _check_deno()


REACT_SSR_SHIM = """
const React = {
    createElement: (type, props, ...children) => {
        if (typeof type === 'function') return type({...props, children: children.flat()});
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

    if (type === React.Fragment) return [].concat(children).map(renderToString).join('');

    const attrStr = Object.entries(attrs)
        .filter(([k, v]) => v != null && v !== false && k !== 'key')
        .map(([k, v]) => {
            const name = k === 'className' ? 'class' : k;
            if (v === true) return name;
            if (k === 'style' && typeof v === 'object') {
                const s = Object.entries(v).map(([p, s]) => `${p.replace(/[A-Z]/g, m => '-'+m.toLowerCase())}:${s}`).join(';');
                return `style="${s}"`;
            }
            return `${name}="${String(v).replace(/"/g, '&quot;')}"`;
        }).join(' ');

    const tag = typeof type === 'string' ? type : 'div';
    const childHtml = [].concat(children).map(renderToString).join('');
    const selfClosing = ['img','br','hr','input','meta','link','area','base','col'];
    if (selfClosing.includes(tag) && !childHtml) return `<${tag}${attrStr ? ' ' + attrStr : ''} />`;
    return `<${tag}${attrStr ? ' ' + attrStr : ''}>${childHtml}</${tag}>`;
}
"""


def _tier2_extract(widget: dict, js_source: str | None = None) -> WidgetContent | None:
    if not js_source:
        return None

    widget_type = widget.get("name?") or widget.get("name")
    props = widget.get("props", {})
    content = WidgetContent(widget_type=widget_type, raw_props=props, extraction_tier=2)
    props_json = json.dumps(props)

    if _check_quickjs():
        try:
            import quickjs
            ctx = quickjs.Context()
            ctx.eval(REACT_SSR_SHIM)
            result = ctx.eval(f"""
                (function() {{
                    const __props = {props_json};
                    {js_source}
                    if (typeof __default !== 'undefined') return renderToString(__default(__props));
                    if (typeof Widget !== 'undefined') return renderToString(Widget(__props));
                    if (typeof render !== 'undefined') return renderToString(render(__props));
                    return '';
                }})()
            """)
            if result:
                content.html = str(result)
                return content
        except Exception as e:
            logger.debug(f"quickjs failed: {e}")

    if _check_deno():
        try:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False) as f:
                f.write(REACT_SSR_SHIM)
                f.write(f"\nconst __props = {props_json};\n")
                f.write(js_source)
                f.write("""
                    let result = '';
                    if (typeof __default !== 'undefined') result = renderToString(__default(__props));
                    else if (typeof Widget !== 'undefined') result = renderToString(Widget(__props));
                    else if (typeof render !== 'undefined') result = renderToString(render(__props));
                    console.log(result);
                """)
                f.flush()
                result = subprocess.run(["deno", "run", f.name], capture_output=True, text=True, timeout=10)
                Path(f.name).unlink(missing_ok=True)
                if result.returncode == 0 and result.stdout.strip():
                    content.html = result.stdout.strip()
                    return content
        except Exception as e:
            logger.debug(f"deno failed: {e}")

    return None


# Public API

def extract_widget_content(
    widget: dict,
    js_source: str | None = None,
    enable_js_execution: bool = False,
) -> WidgetContent:
    """Extract renderable content from a widget."""
    content = _tier0_extract(widget)
    if content and content.has_content:
        return content

    content = _tier1_extract(widget)
    if content.has_content:
        return content

    if enable_js_execution and js_source:
        js_content = _tier2_extract(widget, js_source)
        if js_content and js_content.has_content:
            return js_content

    return WidgetContent(
        widget_type=widget.get("name?") or widget.get("name"),
        raw_props=widget.get("props", {}),
        extraction_tier=-1,
    )


def extract_images(widget: dict) -> list[tuple[str, bytes]]:
    return extract_widget_content(widget).images


def widget_to_html(
    widget: dict,
    js_source: str | None = None,
    enable_js_execution: bool = False,
) -> str:
    return extract_widget_content(widget, js_source, enable_js_execution).to_html()


def extract_images_from_widget_props(props: dict) -> list[tuple[str, bytes]]:
    """Legacy API."""
    return extract_images({"props": props})


class WidgetRenderer:
    """Context manager for JS-based widget rendering."""

    def __enter__(self) -> "WidgetRenderer":
        return self

    def __exit__(self, *args) -> None:
        pass

    def render(self, js_source: str, props: dict) -> str:
        return extract_widget_content({"props": props}, js_source=js_source, enable_js_execution=True).to_html()

    @property
    def backend_name(self) -> str:
        if _check_quickjs():
            return "quickjs"
        if _check_deno():
            return "deno"
        return "none"


def render_widget(js_source: str, props: dict) -> str:
    with WidgetRenderer() as r:
        return r.render(js_source, props)


def is_available() -> bool:
    return True


def get_backend() -> str:
    if _check_quickjs():
        return "quickjs"
    if _check_deno():
        return "deno"
    return "extraction"
