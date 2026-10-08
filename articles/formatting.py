"""
Repairs bold/italic markers that were written across a line break.

Article bodies are stored as a tiny markdown subset (`**bold**`, `*italic*`,
`* ` bullets) that the Android app's formatArticleBody() and the admin
editor's preview both render ONE LINE AT A TIME -- deliberately, so a single
unclosed marker can never swallow the rest of an article. The cost is that a
pair split across lines renders as literal asterisks:

    **1. Getting a good latch
    **

That is exactly what the admin editor used to produce when an admin selected
a whole line (a triple-click selects the line *and* its line break) and pressed
Bold. The editor no longer does that, but this runs on every save anyway, so
hand-typed content and any other client get the same guarantee, and data
migration 0003 applies it once to what is already stored.

Conservative on purpose. A pair is only rewritten when ALL of these hold:
  - its content crosses a line break but not a blank line (never spans paragraphs);
  - its content holds no other asterisk (so it cannot pair with the wrong marker);
  - the opening marker is not followed by a space or tab (that is a `* ` bullet);
  - it has the shape the old editor produced: the closing marker starts its
    line, or the opening marker ends its line, or the pair runs from the start
    of one line's text to the end of another's.
Anything else is left exactly as written. A pair already on one line is never
touched.
"""

import re

_CANDIDATE = re.compile(
    r"(?<!\*)(?P<m>\*{1,3})(?![* \t])"            # opener, not a bullet
    r"(?P<inner>(?:(?!\n[ \t]*\n)[^*])*?\n(?:(?!\n[ \t]*\n)[^*])*?)"
    r"(?<!\*)(?P=m)(?!\*)"                        # same closer
)

_BULLET = re.compile(r"^([ \t]*[*-] )")


def _line_start(text: str, i: int) -> bool:
    """Only whitespace (or a bullet) between the previous line break and i."""
    head = text[text.rfind("\n", 0, i) + 1 : i]
    return head.strip() == "" or _BULLET.fullmatch(head) is not None


def _line_end(text: str, i: int) -> bool:
    """Only whitespace between i and the next line break."""
    nl = text.find("\n", i)
    return text[i : (len(text) if nl == -1 else nl)].strip() == ""


def _rewrap(marker: str, inner: str) -> str:
    """Re-applies `marker` to each non-blank line of `inner`, after any bullet."""
    lines = inner.split("\n")
    # The line the old editor left holding only a marker is now empty -- drop it
    # rather than leave an extra line break behind.
    if lines and not lines[-1].strip():
        lines.pop()
    if lines and not lines[0].strip():
        lines.pop(0)
    out = []
    for line in lines:
        bullet = _BULLET.match(line)
        prefix = bullet.group(1) if bullet else line[: len(line) - len(line.lstrip())]
        core = line[len(prefix) :].strip()
        out.append(f"{prefix}{marker}{core}{marker}" if core else line)
    return "\n".join(out)


def normalize_markers(text: str) -> str:
    """Returns `text` with each line-crossing marker pair re-applied per line."""
    if not text or "*" not in text:
        return text
    src = text.replace("\r\n", "\n")
    out, pos, changed = [], 0, False
    while True:
        m = _CANDIDATE.search(src, pos)
        if not m:
            break
        opener_end = m.start("inner")
        closer_start = m.end("inner")
        fits = (
            _line_start(src, closer_start)
            or _line_end(src, opener_end)
            or (_line_start(src, m.start()) and _line_end(src, m.end()))
        )
        if not fits:
            # Not ours to rewrite. Step past the opener only: the closer may be
            # the opener of a real pair further on.
            out.append(src[pos:opener_end])
            pos = opener_end
            continue
        out.append(src[pos : m.start()])
        out.append(_rewrap(m.group("m"), m.group("inner")))
        pos = m.end()
        changed = True
    if not changed:
        return text  # untouched, original line endings included
    out.append(src[pos:])
    return "".join(out)
