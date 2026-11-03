"""Apply one `mutmut show` diff to app/<module>.py, for run-mutmut.sh --verify.

mutmut 3.8 prints a mutant as a diff of the mutated function on its own:
line numbers count from the function's `def` line (line 1), and a method is
dedented by one level. `patch` can't apply that, so this finds the function
in the module and replaces the hunk at the line it names, comparing lines
without their indentation (mutmut dedents a method's code but not the text
of a multi-line string inside it) and keeping the file's own unchanged
lines. When that line doesn't match (mutmut sometimes counts from a comment
block above the function), it falls back to the one place in or just above
the function where the old lines occur, and gives up when there is no such
single place.

Usage: python dev/mutmut_apply.py <diff-file> <module-file>
Exit 0 when applied, 1 when not (the file is then left untouched).
"""

import re
import sys


def parse(diff_lines):
    """The mutant's (class or None, function), the hunk's start line, and
    its lines as (tag, text) with tag " ", "-" or "+", or None when the
    diff isn't one hunk."""
    head = re.search(r"\.x(?:ǁ(\w+)ǁ|_)(\w+?)__mutmut_\d+", diff_lines[0])
    if not head:
        return None
    hunk, start, hunks = [], None, 0
    for line in diff_lines[1:]:
        if line.startswith("@@"):
            hunks += 1
            start = int(re.match(r"@@ -(\d+)", line).group(1))
            continue
        if start is None or line.startswith(("---", "+++")):
            continue
        tag, body = (line[:1], line[1:]) if line else (" ", "")
        if tag in " -+":
            hunk.append((tag, body))
    if hunks != 1:
        return None
    return head.group(1), head.group(2), start, hunk


def function_span(src, cls, func):
    """First and past-last line index of the function in ``src``, and its
    indent, or None."""
    indent = ""
    lo = 0
    if cls:
        lo = next((i for i, s in enumerate(src) if re.match(rf"class {cls}\b", s)), None)
        if lo is None:
            return None
        indent = "    "
    start = next(
        (i for i in range(lo, len(src)) if re.match(rf"{indent}(async )?def {func}\(", src[i])),
        None,
    )
    if start is None:
        return None
    end = start + 1
    while end < len(src) and (
        not src[end].strip()
        or src[end].startswith(indent + " ")
        or src[end].startswith(indent + ")")
    ):
        end += 1
    return start, end, indent


def apply(diff_lines, src):
    """``src`` with the mutant applied, or None."""
    parsed = parse(diff_lines)
    if parsed is None:
        return None
    cls, func, hunk_start, hunk = parsed
    span = function_span(src, cls, func)
    if span is None:
        return None
    start, end, indent = span
    old = [body.strip() for tag, body in hunk if tag != "+"]

    def matches(i):
        return [line.strip() for line in src[i : i + len(old)]] == old

    at = start + hunk_start - 1
    if not matches(at):
        # The hunk's context can reach above the function: mutmut counts
        # from a comment block right above it, when there is one.
        hits = [i for i in range(max(0, start - 10), end - len(old) + 1) if matches(i)]
        if len(hits) != 1:
            return None
        at = hits[0]
    out, i = [], at
    for tag, body in hunk:
        if tag == " ":
            out.append(src[i])
            i += 1
        elif tag == "-":
            i += 1
        else:
            out.append(indent + body if body else body)
    return src[:at] + out + src[i:]


def main():
    diff_path, target = sys.argv[1], sys.argv[2]
    with open(diff_path, encoding="utf-8") as f:
        diff_lines = f.read().splitlines()
    with open(target, encoding="utf-8") as f:
        src = f.read().split("\n")
    result = apply(diff_lines, src)
    if result is None:
        sys.exit(1)
    with open(target, "w", encoding="utf-8") as f:
        f.write("\n".join(result))


if __name__ == "__main__":
    main()
