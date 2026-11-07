"""Find the headings and tables of a Markdown page with markdown-it-py."""
from dataclasses import dataclass, field
from functools import lru_cache

from markdown_it import MarkdownIt

# CommonMark plus GFM tables. "Title" over a "---" line is not a heading on these pages, and text
# inside blocks is kept raw (no inline parsing), which halves the parse time.
_PARSER = MarkdownIt("commonmark").enable("table").disable(["lheading", "inline"])


@dataclass
class Table:
    end: int  # the line after the table
    header: list[str] = field(default_factory=list)
    rows: list[tuple[int, list[str]]] = field(default_factory=list)  # (line number, cells) per body row


@dataclass
class Blocks:
    headings: dict[int, tuple[int, str]] = field(default_factory=dict)  # line -> (level, title), top level only
    tables: dict[int, Table] = field(default_factory=dict)  # line of the header row -> table


@lru_cache(maxsize=None)
def parse(markdown: str) -> Blocks:
    """Index a page's headings and tables by line number; code blocks are never read as either.

    Line numbers count str.splitlines() lines. Pages repeat across snapshots and both indexes read
    each one, so a page is parsed once.
    """
    blocks = Blocks()
    tokens = _PARSER.parse("\n".join(markdown.splitlines()))
    table = None
    cells: list[str] = []
    for index, token in enumerate(tokens):
        if token.type == "heading_open" and token.level == 0:
            blocks.headings[token.map[0]] = (int(token.tag[1:]), tokens[index + 1].content)
        elif token.type == "table_open":
            table = blocks.tables[token.map[0]] = Table(end=token.map[1])
        elif table is not None and token.type == "tr_open":
            cells = []
            if table.header:
                table.rows.append((token.map[0], cells))
        elif table is not None and token.type == "inline":
            cells.append(token.content)
        elif table is not None and token.type == "tr_close" and not table.header:
            table.header = cells
        elif token.type == "table_close":
            table = None
    return blocks
