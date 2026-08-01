#!/usr/bin/env python3
"""Convert every .txt file beside this script into an HTML fragment.

Each source file is written to the same directory with the same basename and
an ``.html`` extension. For example, ``ciudad.txt`` becomes ``ciudad.html``.

Link routing:
  :gazetteer:<numeric_id>  -> ../place/<numeric_id>
  :fuentes:<id>            -> ../fuentes/<id>
  :<id>                    -> ../territory/<id>
  <direct_id>              -> <direct_id> (same directory)

For a non-root DokuWiki namespace such as ``conceptos:cabildo``, the final
page id (``cabildo``) is treated as the direct id.
"""

from __future__ import annotations

import html
import re
from pathlib import Path

SCRIPT_VERSION = "batch-2026-07-28"


class WikiFragmentConverter:
    def __init__(self) -> None:
        self.footnotes: list[str] = []

    @staticmethod
    def make_href(target: str) -> str:
        target = target.strip()
        if target.startswith(":gazetteer:"):
            return "../place/" + target[len(":gazetteer:") :]
        if target.startswith(":fuentes:"):
            return "../fuentes/" + target[len(":fuentes:") :]
        if target.startswith(":"):
            return "../territory/" + target[1:]
        return target.rsplit(":", 1)[-1]

    def inline_markup(self, raw: str) -> str:
        # Repair the accidental four-opening-bracket link found in the source.
        raw = raw.replace("[[[[:", "[[:")

        placeholders: dict[str, str] = {}
        counter = 0

        def stash(value: str) -> str:
            nonlocal counter
            token = f"\x00{counter}\x00"
            counter += 1
            placeholders[token] = value
            return token

        def footnote_repl(match: re.Match[str]) -> str:
            content = match.group(1).strip()
            self.footnotes.append(content)
            number = len(self.footnotes)
            return stash(
                f'<sup class="footnote-ref"><a href="#fn-{number}" '
                f'id="fnref-{number}" aria-label="Footnote {number}">{number}</a></sup>'
            )

        raw = re.sub(r"\(\((.+?)\)\)", footnote_repl, raw)

        def link_repl(match: re.Match[str]) -> str:
            target = match.group(1).strip()
            label = (match.group(2) or target.rsplit(":", 1)[-1]).strip()
            href = self.make_href(target)
            return stash(
                f'<a href="{html.escape(href, quote=True)}">{html.escape(label)}</a>'
            )

        raw = re.sub(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]", link_repl, raw)
        escaped = html.escape(raw, quote=False)
        escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
        escaped = re.sub(r"//(.+?)//", r"<em>\1</em>", escaped)

        for token, value in placeholders.items():
            escaped = escaped.replace(token, value)
        return escaped

    @staticmethod
    def heading_level(eq_count: int) -> int:
        if eq_count >= 6:
            return 1
        return min(6, 7 - eq_count)

    @staticmethod
    def split_table_row(line: str, delimiter: str) -> list[str]:
        trimmed = line.strip()
        if trimmed.startswith(delimiter):
            trimmed = trimmed[1:]
        if trimmed.endswith("|") or trimmed.endswith("^"):
            trimmed = trimmed[:-1]
        return [cell.strip() for cell in trimmed.split("|")]

    def convert(self, text: str) -> str:
        self.footnotes.clear()
        lines = text.splitlines()
        out: list[str] = ['<article class="wiki-entry">']
        paragraph: list[str] = []
        in_wrap = False
        i = 0

        def flush_paragraph() -> None:
            if not paragraph:
                return
            content = " ".join(part.strip() for part in paragraph).strip()
            if content:
                out.append(f"  <p>{self.inline_markup(content)}</p>")
            paragraph.clear()

        while i < len(lines):
            line = lines[i]
            stripped = line.strip()

            if not stripped:
                flush_paragraph()
                i += 1
                continue

            if stripped.startswith("<WRAP"):
                flush_paragraph()
                out.append('  <aside class="wiki-box">')
                in_wrap = True
                remainder = stripped[stripped.find(">") + 1 :].strip()
                if remainder:
                    paragraph.append(remainder)
                i += 1
                continue

            if stripped == "</WRAP>":
                flush_paragraph()
                out.append("  </aside>")
                in_wrap = False
                i += 1
                continue

            heading = re.fullmatch(r"(={2,})\s*(.*?)\s*\1", stripped)
            if heading:
                flush_paragraph()
                level = self.heading_level(len(heading.group(1)))
                out.append(f"  <h{level}>{self.inline_markup(heading.group(2))}</h{level}>")
                i += 1
                continue

            if re.match(r"^\s*\*\s+", line):
                flush_paragraph()
                out.append("  <ul>")
                while i < len(lines) and re.match(r"^\s*\*\s+", lines[i]):
                    item = re.sub(r"^\s*\*\s+", "", lines[i]).strip()
                    out.append(f"    <li>{self.inline_markup(item)}</li>")
                    i += 1
                out.append("  </ul>")
                continue

            if stripped.startswith("^") or (stripped.startswith("|") and stripped.endswith("|")):
                flush_paragraph()
                header_cells: list[str] | None = None
                body_rows: list[list[str]] = []

                if stripped.startswith("^"):
                    header_cells = self.split_table_row(stripped, "^")
                    i += 1

                while i < len(lines):
                    row = lines[i].strip()
                    if not (row.startswith("|") and row.endswith("|")):
                        break
                    body_rows.append(self.split_table_row(row, "|"))
                    i += 1

                out.extend(['  <div class="table-wrapper">', "    <table>"])
                if header_cells is not None:
                    out.extend(["      <thead>", "        <tr>"])
                    out.extend(
                        f'          <th scope="col">{self.inline_markup(cell)}</th>'
                        for cell in header_cells
                    )
                    out.extend(["        </tr>", "      </thead>"])
                out.append("      <tbody>")
                for row in body_rows:
                    out.append("        <tr>")
                    out.extend(f"          <td>{self.inline_markup(cell)}</td>" for cell in row)
                    out.append("        </tr>")
                out.extend(["      </tbody>", "    </table>", "  </div>"])
                continue

            # A line enclosed in //...// is rendered as a quotation rather than
            # merely inline emphasis.
            if stripped.startswith("//") and "//" in stripped[2:]:
                close = stripped.find("//", 2)
                quote_text = stripped[2:close]
                tail = stripped[close + 2 :].strip()

                # Repair the source's one missing opening parenthesis in the
                # Zamora citation, while leaving the citation text untouched.
                if re.fullmatch(r"\([^()]*(?:\([^()]*\)[^()]*)*\)\)", tail):
                    tail = "(" + tail

                flush_paragraph()
                quote_html = self.inline_markup(quote_text)
                if tail:
                    quote_html += " " + self.inline_markup(tail)
                out.append(f"  <blockquote><p>{quote_html}</p></blockquote>")
                i += 1
                continue

            paragraph.append(stripped)
            i += 1

        flush_paragraph()
        if in_wrap:
            out.append("  </aside>")

        if self.footnotes:
            out.extend(
                [
                    '  <section class="footnotes" aria-label="Footnotes">',
                    "    <ol>",
                ]
            )
            for number, note in enumerate(self.footnotes, start=1):
                out.append(
                    f'      <li id="fn-{number}">{html.escape(note)} '
                    f'<a href="#fnref-{number}" class="footnote-backref" '
                    f'aria-label="Back to reference {number}">↩</a></li>'
                )
            out.extend(["    </ol>", "  </section>"])

        out.append("</article>")
        return "\n".join(out) + "\n"


def main() -> None:
    directory = Path(__file__).resolve().parent
    source_files = sorted(
        path for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() == ".txt"
    )

    print(f"wiki_to_html.py {SCRIPT_VERSION}")
    print(f"Source/output directory: {directory}")

    if not source_files:
        print(f"No .txt files found in {directory}")
        return

    converter = WikiFragmentConverter()
    converted_count = 0

    for source_path in source_files:
        output_path = source_path.with_suffix(".html")
        try:
            source_text = source_path.read_text(encoding="utf-8")
            converted = converter.convert(source_text)
            output_path.write_text(converted, encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            print(f"Failed: {source_path.name}: {exc}")
            continue

        print(f"Converted: {source_path.name} -> {output_path.name}")
        converted_count += 1

    print(f"Converted {converted_count} of {len(source_files)} text file(s).")


if __name__ == "__main__":
    main()
