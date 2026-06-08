import markdown
import bleach

# Allowed tags when rendering report markdown to HTML (safe subset for prose)
REPORT_HTML_TAGS = [
    'p', 'br', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
    'ul', 'ol', 'li', 'strong', 'em', 'code', 'pre', 'blockquote',
    'a', 'table', 'thead', 'tbody', 'tr', 'th', 'td', 'hr', 'div', 'span',
]
REPORT_HTML_ATTRS = {'a': ['href', 'title']}


def markdown_to_safe_html(markdown_text: str) -> str:
    """Convert report markdown to sanitized HTML"""
    raw_html = markdown.markdown(
        markdown_text or '',
        extensions=['fenced_code', 'tables', 'nl2br'],
    )
    allowed_protocols = ['http', 'https', 'mailto']
    return bleach.clean(raw_html, tags=REPORT_HTML_TAGS, attributes=REPORT_HTML_ATTRS, protocols=allowed_protocols, strip=True)