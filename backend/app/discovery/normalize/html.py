# ruff: noqa: RUF001
import html as _html
import re

import nh3

_TAGS = {
    "p", "br", "ul", "ol", "li", "strong", "b", "em", "i", "u", "h1", "h2", "h3", "h4", "h5", "h6",
    "a", "blockquote", "code", "pre", "hr", "div", "span", "sub", "sup",
}  # fmt: skip
_ATTRS = {"a": {"href", "title"}}
_SCHEMES = {"http", "https", "mailto"}


def sanitize_html(value: str | None) -> str | None:
    """Untrusted HTML -> safe subset (basic formatting + links)."""
    if value is None:
        return None
    cleaned = nh3.clean(
        value, tags=_TAGS, attributes=_ATTRS, url_schemes=_SCHEMES, link_rel="noopener noreferrer nofollow"
    )
    return cleaned.strip() or None


def html_to_text(value: str | None) -> str | None:
    if not value:
        return None
    s = re.sub(r"<\s*br\s*/?\s*>", "\n", value, flags=re.I)
    s = re.sub(r"<\s*li[^>]*>", "\n- ", s, flags=re.I)
    s = re.sub(r"</\s*(?:p|div|h[1-6]|tr|ul|ol|blockquote|pre)\s*>", "\n", s, flags=re.I)
    s = nh3.clean(s, tags=set())
    s = _html.unescape(s)
    lines = [re.sub(r"[ \t ]+", " ", ln).strip() for ln in s.splitlines()]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return text or None


def text_to_html(value: str | None) -> str | None:
    """Plain text -> minimal HTML paragraphs (escaped)."""
    if not value or not value.strip():
        return None
    paras = [p.strip() for p in re.split(r"\n\s*\n", value.strip()) if p.strip()]
    return "".join(f"<p>{_html.escape(p).replace(chr(10), '<br>')}</p>" for p in paras)
