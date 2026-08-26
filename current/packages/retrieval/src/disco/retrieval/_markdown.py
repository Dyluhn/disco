"""Minimal stdlib HTML-to-markdown conversion for bundled extraction."""

from __future__ import annotations

from html.parser import HTMLParser


class MarkdownExtractor(HTMLParser):
    """Drop navigation noise and turn basic HTML blocks into markdown."""

    _SKIP = {"script", "style", "noscript", "nav", "footer", "svg"}
    _BLOCK = {"p", "div", "section", "article", "br", "li", "tr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.title = ""
        self._skip_depth = 0
        self._in_title = False
        self._href: str | None = None
        self._a_mark = -1

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.out.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag == "li":
            self.out.append("\n- ")
        elif tag in self._BLOCK:
            self.out.append("\n")
        elif tag == "a":
            self._href = dict(attrs).get("href")
            self._a_mark = len(self.out)
            self.out.append("[")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False
        elif tag == "a":
            href = self._href or ""
            inner = "".join(self.out[self._a_mark + 1 :]).strip()
            if inner:
                self.out.append(f"]({href})" if href else "]")
            else:
                del self.out[self._a_mark :]
            self._href = None
            self._a_mark = -1
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.out.append("\n")

    def handle_data(self, data):
        if self._skip_depth:
            return
        if self._in_title:
            self.title += data.strip() + " "
            return
        text = data.replace("\xa0", " ")
        if text.strip() or text == " ":
            self.out.append(text)

    def markdown(self) -> str:
        lines = [line.rstrip() for line in "".join(self.out).splitlines()]
        output: list[str] = []
        blanks = 0
        for line in lines:
            stripped = line.strip()
            if stripped in ("-", "*", "•"):
                continue
            if not stripped:
                blanks += 1
                if blanks <= 1:
                    output.append("")
            else:
                blanks = 0
                output.append(line)
        return "\n".join(output).strip()
