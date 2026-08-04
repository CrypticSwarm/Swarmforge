"""A dependency-free parser for the YAML subset Swarmforge configuration uses.

The subset is nested maps of scalars plus flat lists -- enough for agent
frontmatter and for tong definitions, and small enough that both the host
launcher and the harness image can read it without a third-party package.

This is a leaf module: it imports nothing else from the package, so either side
of the container boundary can depend on it without dragging the rest along.
"""


def parse_scalar(text):
    text = text.strip()
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [parse_scalar(part) for part in inner.split(",")]
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    lowered = text.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered in ("", "null", "~"):
        return None
    for converter in (int, float):
        try:
            return converter(text)
        except ValueError:
            pass
    return text


def strip_inline_comment(text):
    """Drop a trailing `#` comment from a value.

    A `#` opens a comment only at the start of the value or after whitespace, so
    an image digest or a URL fragment keeps its own. Quoted runs are skipped, and
    only a quote that *opens* a value starts one -- an apostrophe in prose is
    ordinary text. Both YAML escapes are honored inside a run, since reading one
    as the closing quote would truncate the value at a `#` that is really data.
    """
    i = 0
    at_value_start = True
    while i < len(text):
        char = text[i]
        if at_value_start and char in "\"'":
            quote = char
            i += 1
            while i < len(text):
                if quote == '"' and text[i] == "\\":
                    i += 2  # a backslash escapes the next character
                    continue
                if text[i] == quote:
                    if quote == "'" and text[i + 1:i + 2] == "'":
                        i += 2  # a doubled quote is an escape, not the end
                        continue
                    i += 1
                    break
                i += 1
            at_value_start = False
            continue
        if char == "#" and (i == 0 or text[i - 1] in " \t"):
            return text[:i]
        if char not in " \t":
            # A flow list opens a fresh value after `[` and after each `,`.
            at_value_start = char in "[,"
        i += 1
    return text


def is_comment_or_blank(line):
    """True for a line carrying no structure, which layout decisions must ignore."""
    stripped = line.strip()
    return not stripped or stripped.startswith("#")


def parse_map(lines, index, indent):
    out = {}
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if is_comment_or_blank(line):
            index += 1
            continue
        current = len(line) - len(line.lstrip(" "))
        if current < indent:
            break
        if current > indent or stripped.startswith("- "):
            raise ValueError("unexpected layout at line: %r" % line)
        key, sep, rest = stripped.partition(":")
        if not sep:
            raise ValueError("expected 'key: value' at line: %r" % line)
        key = key.strip()
        rest = strip_inline_comment(rest).strip()
        index += 1
        if rest:
            out[key] = parse_scalar(rest)
            continue
        # The first line carrying structure decides whether the block below is a
        # list or a map, and at what indent; a comment must decide neither.
        peek = index
        while peek < len(lines) and is_comment_or_blank(lines[peek]):
            peek += 1
        if peek < len(lines):
            next_indent = len(lines[peek]) - len(lines[peek].lstrip(" "))
            if next_indent > indent:
                if lines[peek].strip().startswith("- "):
                    out[key], index = parse_list(lines, peek, next_indent)
                else:
                    out[key], index = parse_map(lines, peek, next_indent)
                continue
        out[key] = None
    return out, index


def parse_list(lines, index, indent):
    out = []
    while index < len(lines):
        line = lines[index]
        # A comment between two items does not end the list.
        if is_comment_or_blank(line):
            index += 1
            continue
        current = len(line) - len(line.lstrip(" "))
        if current != indent or not line.strip().startswith("- "):
            break
        out.append(parse_scalar(strip_inline_comment(line.strip()[2:])))
        index += 1
    return out, index
