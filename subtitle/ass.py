"""Subtitle frame-rate conversion and ASS export.

The helpers here are deliberately dependency free.  They cover the pieces a
subtitle pipeline needs when an SRT track has to be retimed to a different
frame rate and written out as Advanced SubStation Alpha (ASS):

* SRT timecode parsing (comma or dot, optional BOM);
* frame based FPS conversion (no cumulative drift);
* overlap resolution onto separate ASS layers;
* SRT/HTML inline tag conversion to ASS override tags;
* ASS timecode formatting;
* a valid ``[Script Info]`` / ``[V4+ Styles]`` / ``[Events]`` document.

All public helpers return new objects; the input list is never mutated.
"""

from __future__ import annotations

import codecs
import html
import math
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from fractions import Fraction

# ---------------------------------------------------------------------------
# Timecode helpers
# ---------------------------------------------------------------------------

_DAY_SECONDS = 86400.0
_TIME_RE = re.compile(
    r"^\s*(?:(?P<hours>\d+)\s*:)?(?P<minutes>\d{1,2})\s*:"
    r"(?P<seconds>\d{1,2})(?P<frac>[,.]\d{1,3})?\s*$"
)
_BOM = "﻿"


def parse_srt_time(text):
    """Parse an SRT/ASS timestamp into seconds.

    Accepts ``HH:MM:SS,mmm`` and ``HH:MM:SS.mmm`` (and ``MM:SS,mmm``),
    tolerates a leading UTF-8 BOM and surrounding whitespace, and also
    accepts one or two fractional digits.  ``TypeError``/``ValueError`` are
    raised for values that are not timestamps.
    """

    if isinstance(text, bytes):
        text = decode_subtitle_bytes(text)

    if not isinstance(text, str):
        raise TypeError(f"timecode must be str, got {type(text).__name__}")

    value = text.replace(_BOM, "").strip()
    match = _TIME_RE.match(value)
    if match is None:
        raise ValueError(f"invalid subtitle timecode: {text!r}")

    hours = int(match.group("hours") or 0)
    minutes = int(match.group("minutes"))
    seconds = int(match.group("seconds"))
    frac_text = match.group("frac")
    fraction = 0.0
    if frac_text:
        digits = frac_text[1:]
        # SRT uses milliseconds, ASS uses centiseconds; normalise by digit
        # count so both "1,5", "1,50" and "1,500" mean half a second.
        fraction = int(digits) / (10 ** len(digits))

    return hours * 3600.0 + minutes * 60.0 + seconds + fraction


def format_ass_time(seconds):
    """Format seconds as an ASS timestamp: ``H:MM:SS.cc`` (dot, 2 digits)."""

    total_cs = _to_centiseconds(seconds)
    sign = "-" if total_cs < 0 else ""
    total_cs = abs(total_cs)

    hours, remainder = divmod(total_cs, 360000)
    minutes, remainder = divmod(remainder, 6000)
    secs, centis = divmod(remainder, 100)
    return f"{sign}{hours}:{minutes:02d}:{secs:02d}.{centis:02d}"


def format_srt_time(seconds):
    """Format seconds as an SRT timestamp: ``HH:MM:SS,mmm`` (comma)."""

    total_ms = _to_milliseconds(seconds)
    sign = "-" if total_ms < 0 else ""
    total_ms = abs(total_ms)

    hours, remainder = divmod(total_ms, 3600000)
    minutes, remainder = divmod(remainder, 60000)
    secs, millis = divmod(remainder, 1000)
    return f"{sign}{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def format_timecode(seconds, target="ass"):
    """Format a timestamp for ``target`` (``"ass"`` or ``"srt"``)."""

    target = str(target).lower()
    if target in ("ass", "ssa"):
        return format_ass_time(seconds)
    if target == "srt":
        return format_srt_time(seconds)
    raise ValueError(f"unsupported timecode target: {target!r}")


def signed_duration(start, end, wrap_midnight=True):
    """Return ``end - start`` in seconds, correcting a midnight rollover.

    SRT files commonly end a cue at ``00:00:01,000`` when it started at
    ``23:59:59,000``.  Subtracting naively gives a large negative number;
    with ``wrap_midnight`` the duration is normalised by adding whole days.
    Non-negative deltas are returned unchanged.
    """

    delta = float(end) - float(start)
    if wrap_midnight and delta < 0:
        days = math.ceil(-delta / _DAY_SECONDS)
        delta += days * _DAY_SECONDS
    return delta


def _to_centiseconds(seconds):
    """Round seconds to centiseconds using round-half-up, not banker's."""

    try:
        value = Decimal(str(seconds))
    except (InvalidOperation, ValueError):
        value = Decimal(str(float(seconds)))
    return int((value * 100).quantize(Decimal("1"), rounding="ROUND_HALF_UP"))


def _to_milliseconds(seconds):
    try:
        value = Decimal(str(seconds))
    except (InvalidOperation, ValueError):
        value = Decimal(str(float(seconds)))
    return int((value * 1000).quantize(Decimal("1"), rounding="ROUND_HALF_UP"))


# ---------------------------------------------------------------------------
# Frame-rate conversion
# ---------------------------------------------------------------------------


def _parse_fps(value):
    """Convert an FPS value to an exact ``Fraction`` when possible."""

    if isinstance(value, Fraction):
        return value
    if isinstance(value, str):
        text = value.strip()
        if "/" in text:
            return Fraction(text)
        return Fraction(text)
    # str() keeps decimal literals such as 23.976 exact instead of picking up
    # the binary representation of the float.
    return Fraction(str(value))


def _frames_from_time(seconds, fps):
    """Snap a timestamp to the nearest source frame (round half up)."""

    exact = Fraction(str(float(seconds))) * fps
    return int(math.floor(exact + Fraction(1, 2)))


def convert_fps(subtitles, source_fps, target_fps):
    """Retime cues from ``source_fps`` to ``target_fps``.

    A subtitle is authored against a frame grid.  Converting it means keeping
    the *frame number* and recalculating the wall-clock time at the target
    rate, i.e. ``new_time = round(time * source_fps) / target_fps``.  Doing
    the rounding per cue from the original timestamp prevents the cumulative
    drift that a plain ``time * source/target`` ratio produces on long films.
    """

    source = _parse_fps(source_fps)
    target = _parse_fps(target_fps)
    if source <= 0 or target <= 0:
        raise ValueError("frame rates must be positive")

    result = [dict(item) for item in subtitles]
    if source == target:
        return result

    for item in result:
        for key in ("start", "end"):
            if key not in item or item[key] is None:
                continue
            frames = _frames_from_time(item[key], source)
            item[key] = float(Fraction(frames, 1) / target)
    return result


# ---------------------------------------------------------------------------
# Overlap / layer handling
# ---------------------------------------------------------------------------


def _spans_overlap(a_start, a_end, b_start, b_end):
    # Touching spans ([0,1] and [1,2]) are adjacent, not overlapping.
    return a_start < b_end and b_start < a_end


def resolve_overlaps(subtitles):
    """Assign overlapping cues to distinct ASS layers.

    Cues are considered in chronological order.  Each cue keeps its requested
    base layer when that layer is free; otherwise it is promoted to the next
    free layer.  This is what makes bilingual subtitles visible instead of
    letting a same-layer cue paint over the previous one.  Returned cues keep
    the original list order and timestamps.
    """

    items = [dict(item) for item in subtitles]
    order = sorted(
        range(len(items)),
        key=lambda i: (
            float(items[i].get("start", 0.0)),
            float(items[i].get("end", 0.0)),
            i,
        ),
    )

    occupied = {}

    for index in order:
        item = items[index]
        start = float(item.get("start", 0.0))
        end = float(item.get("end", start))
        if end < start:
            end = start + signed_duration(start, end)

        try:
            base_layer = int(item.get("layer", 0) or 0)
        except (TypeError, ValueError):
            base_layer = 0
        base_layer = max(0, base_layer)

        layer = base_layer
        while any(
            _spans_overlap(start, end, used_start, used_end)
            for used_start, used_end in occupied.get(layer, ())
        ):
            layer += 1

        occupied.setdefault(layer, []).append((start, end))
        item["layer"] = layer
        item["start"] = start
        item["end"] = end

    return items


# ---------------------------------------------------------------------------
# SRT inline tag handling
# ---------------------------------------------------------------------------

_HTML_TAG_RE = re.compile(r"<[^>]*>")
_BR_RE = re.compile(r"<\s*br\s*/?\s*>", re.IGNORECASE)
_FONT_ATTR_RE = re.compile(
    r"""([\w:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))"""
)

_NAMED_COLORS = {
    "black": "000000",
    "silver": "C0C0C0",
    "gray": "808080",
    "grey": "808080",
    "white": "FFFFFF",
    "maroon": "800000",
    "red": "FF0000",
    "purple": "800080",
    "fuchsia": "FF00FF",
    "magenta": "FF00FF",
    "green": "008000",
    "lime": "00FF00",
    "olive": "808000",
    "yellow": "FFFF00",
    "navy": "000080",
    "blue": "0000FF",
    "teal": "008080",
    "aqua": "00FFFF",
    "cyan": "00FFFF",
}


def strip_srt_tags(text):
    """Return the plain text of an SRT cue with inline markup removed."""

    if text is None:
        return ""
    if isinstance(text, bytes):
        text = decode_subtitle_bytes(text)
    text = normalize_newlines(text)
    text = _BR_RE.sub("\n", text)
    text = _HTML_TAG_RE.sub("", text)
    # Unescape last so an entity that expands to "<" is not mistaken for a tag.
    text = html.unescape(text)
    return text.replace("\xa0", " ")


def srt_tags_to_ass(text):
    """Convert SRT/HTML inline tags to ASS override blocks.

    ``<i>``/``<b>``/``<u>``/``<s>`` become ``{\\i1}`` etc.; ``<font>`` colour,
    face and size become ``{\\c...}``, ``{\\fn...}`` and ``{\\fs...}``.
    Closing tags restore the previous state, so nested markup survives the
    conversion instead of leaking HTML into the rendered ASS text.
    """

    if text is None:
        return ""
    if isinstance(text, bytes):
        text = decode_subtitle_bytes(text)

    text = normalize_newlines(text)
    # Preserve explicit line breaks produced by SRT's <br> variants.
    text = _BR_RE.sub("\n", text)

    font_stack = []
    output = []

    def emit(tag):
        output.append("{" + tag + "}")

    def styled_emit(fragment):
        """Append visible text, escaping newlines to ASS ``\\N``."""
        if not fragment:
            return
        fragment = html.unescape(fragment).replace("\xa0", " ")
        output.append(fragment.replace("\n", "\\N"))

    for piece in re.split(r"(<[^>]*>)", text):
        if not piece:
            continue
        if not piece.startswith("<"):
            styled_emit(piece)
            continue

        tag = piece[1:-1].strip()
        if not tag:
            continue
        if tag.startswith("/"):
            name = tag[1:].strip().lower().split()[0] if tag[1:].strip() else ""
            if name in ("i", "em"):
                emit("\\i0")
            elif name in ("b", "strong"):
                emit("\\b0")
            elif name == "u":
                emit("\\u0")
            elif name in ("s", "strike", "del"):
                emit("\\s0")
            elif name == "font":
                # Restore the enclosing <font> state (not the one being
                # closed).  An empty value means "back to the style default",
                # which ASS accepts as a bare override.
                if font_stack:
                    font_stack.pop()
                previous = font_stack[-1] if font_stack else {}
                if previous.get("color"):
                    emit("\\c" + _ass_color(previous["color"]))
                else:
                    emit("\\c")
                emit("\\fn" + str(previous.get("face") or ""))
                emit("\\fs" + str(previous.get("size") or ""))
            # Unknown closing tags are dropped.
            continue

        name = tag.split()[0].lower() if tag.split() else ""
        if name in ("i", "em"):
            emit("\\i1")
        elif name in ("b", "strong"):
            emit("\\b1")
        elif name == "u":
            emit("\\u1")
        elif name in ("s", "strike", "del"):
            emit("\\s1")
        elif name == "font":
            attrs = {}
            for attr_match in _FONT_ATTR_RE.finditer(tag[4:]):
                key = attr_match.group(1).lower()
                attrs[key] = next(
                    (g for g in attr_match.groups()[1:] if g is not None), ""
                )
            font_stack.append(
                {
                    "color": attrs.get("color"),
                    "face": attrs.get("face"),
                    "size": attrs.get("size"),
                }
            )
            if attrs.get("color"):
                emit("\\c" + _ass_color(attrs["color"]))
            if attrs.get("face"):
                emit("\\fn" + attrs["face"])
            if attrs.get("size"):
                emit("\\fs" + str(attrs["size"]).strip())
        # Unknown opening tags are dropped (they would render as garbage).

    return "".join(output)


def _ass_color(value, default="&H00FFFFFF&"):
    """Convert ``#RRGGBB``/named colours to ASS ``&HAABBGGRR&`` notation."""

    if value is None:
        return default
    text = str(value).strip()
    if not text:
        return default
    if text.startswith("&H") or text.startswith("&h"):
        body = text[2:].rstrip("&")
        return f"&H{body.upper()}&"
    if text.startswith("#"):
        text = text[1:]
    if text.lower() in _NAMED_COLORS:
        text = _NAMED_COLORS[text.lower()]
    if len(text) == 3:
        text = "".join(char * 2 for char in text)
    if len(text) == 6 and re.fullmatch(r"[0-9a-fA-F]{6}", text):
        # CSS is #RRGGBB, ASS is &HAABBGGRR&; keep full opacity.
        rr, gg, bb = text[0:2], text[2:4], text[4:6]
        return "&H00{}{}{}&".format(bb, gg, rr).upper()
    return default


# ---------------------------------------------------------------------------
# Safe-area wrapping
# ---------------------------------------------------------------------------


def _tokenize_ass_text(text):
    """Split text into tags (zero width), words, wide chars and breaks."""

    tokens = []
    index = 0
    length = len(text)

    def is_break(char, position):
        return char == "\n" or (
            char == "\\"
            and position + 1 < length
            and text[position + 1] in ("N", "n")
        )

    while index < length:
        char = text[index]

        if char == "{":
            close = text.find("}", index + 1)
            if close != -1:
                tokens.append(("tag", text[index : close + 1], 0.0))
                index = close + 1
                continue
            # Unbalanced brace: it is literal text, not a broken override.
            tokens.append(("text", char, 0.5))
            index += 1
            continue

        if is_break(char, index):
            tokens.append(("break", "", 0.0))
            index += 2 if char == "\\" else 1
            continue

        if char.isspace():
            tokens.append(("space", " ", 0.5))
            index += 1
            continue

        if unicodedata.east_asian_width(char) in ("W", "F"):
            tokens.append(("text", char, 1.0))
            index += 1
            continue

        start = index
        while index < length:
            char = text[index]
            if text[index].isspace():
                break
            if unicodedata.east_asian_width(char) in ("W", "F"):
                break
            if is_break(char, index):
                break
            if char == "{" and text.find("}", index + 1) != -1:
                break
            index += 1
        if index == start:
            # Lone backslash or other delimiter: keep it as a literal glyph.
            index += 1
        word = text[start:index]
        tokens.append(("text", word, len(word) * 0.5))

    return tokens


# Closing punctuation may not begin a line: glue it to the preceding token so
# the wrapper never leaves a stray "。" or "，" at the start of a line.
_NO_LINE_START = "，。、；：？！）】》」』〉”’…—～·%.,;:!?)]}"


def _glue_no_line_start(tokens):
    merged = []
    for token in tokens:
        if (
            token[0] == "text"
            and token[1][:1] in _NO_LINE_START
            and merged
            and merged[-1][0] == "text"
        ):
            kind, value, width = merged[-1]
            merged[-1] = (kind, value + token[1], width + token[2])
        else:
            merged.append(token)
    return merged


def wrap_ass_text(text, play_res=(1920, 1080), font_size=48, margin_ratio=0.05):
    """Wrap ASS dialogue text so it stays inside the horizontal safe area.

    Override blocks are zero width, Latin text breaks at spaces and CJK text
    may break between characters.  Existing ``\\N`` breaks are preserved.
    """

    if not text:
        return ""

    try:
        width = float(play_res[0])
    except (TypeError, ValueError, IndexError):
        width = 1920.0
    try:
        font_size = float(font_size)
    except (TypeError, ValueError):
        font_size = 48.0
    if font_size <= 0:
        font_size = 48.0

    safe_width = max(1.0, width * (1.0 - 2.0 * margin_ratio))
    max_em = safe_width / font_size

    lines = []
    current = []
    current_width = 0.0
    has_text = False

    def flush():
        nonlocal current, current_width, has_text
        lines.append("".join(current).rstrip())
        current = []
        current_width = 0.0
        has_text = False

    for kind, value, width_em in _glue_no_line_start(_tokenize_ass_text(text)):
        if kind == "break":
            flush()
            continue
        if kind == "tag":
            current.append(value)
            continue
        if kind == "space":
            if not has_text:
                continue
            if current and current[-1] != " ":
                current.append(" ")
                current_width += width_em
            continue

        if has_text and current_width + width_em > max_em:
            flush()
        current.append(value)
        current_width += width_em
        has_text = True

    lines.append("".join(current).rstrip())

    # Remove empty lines that can appear from consecutive explicit breaks,
    # but keep one if the subtitle is intentionally blank.
    compact = [line for line in lines if line.strip()]
    return "\\N".join(compact) if compact else ""


# ---------------------------------------------------------------------------
# ASS document construction
# ---------------------------------------------------------------------------

_STYLE_FIELDS = (
    "Name",
    "Fontname",
    "Fontsize",
    "PrimaryColour",
    "SecondaryColour",
    "OutlineColour",
    "BackColour",
    "Bold",
    "Italic",
    "Underline",
    "StrikeOut",
    "ScaleX",
    "ScaleY",
    "Spacing",
    "Angle",
    "BorderStyle",
    "Outline",
    "Shadow",
    "Alignment",
    "MarginL",
    "MarginR",
    "MarginV",
    "Encoding",
)

_EVENT_FORMAT = (
    "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
    "Effect, Text"
)

_DEFAULT_STYLE = {
    "name": "Default",
    "font": "Arial",
    "font_size": 48,
    "primary": "&H00FFFFFF&",
    "secondary": "&H000000FF&",
    "outline_color": "&H00000000&",
    "back_color": "&H00000000&",
    "bold": 0,
    "italic": 0,
    "underline": 0,
    "strikeout": 0,
    "scale_x": 100,
    "scale_y": 100,
    "spacing": 0,
    "angle": 0,
    "border_style": 1,
    "outline": 2,
    "shadow": 0,
    "alignment": 2,
    "margin_l": 10,
    "margin_r": 10,
    "margin_v": 10,
    "encoding": 1,
}


def _quote_font(name):
    """Quote an ASS font name when it contains spaces or field separators."""

    name = str(name or "").strip()
    if name.startswith('"') and name.endswith('"') and len(name) >= 2:
        return name
    if not name:
        return '""'
    if any(char in name for char in ' ,\t'):
        return '"' + name.replace('"', "") + '"'
    return name


def _flag(value):
    if isinstance(value, str):
        return 1 if value.strip().lower() in ("1", "true", "yes", "on") else 0
    return 1 if value else 0


def _style_value(style, *keys, default=None):
    for key in keys:
        if key in style and style[key] is not None:
            return style[key]

    # Also accept ASS's own spellings and case differences: Fontname,
    # PrimaryColour, MarginL, ...
    folded = {
        re.sub(r"[^a-z0-9]", "", str(existing).lower()): value
        for existing, value in style.items()
        if value is not None
    }
    for key in keys:
        normalised = re.sub(r"[^a-z0-9]", "", str(key).lower())
        if normalised in folded:
            return folded[normalised]
    return default


def _normalise_style(style):
    merged = dict(_DEFAULT_STYLE)
    if not isinstance(style, dict):
        return merged

    mapping = {
        "name": ("name", "style"),
        "font": ("font", "fontname", "font_name"),
        "font_size": ("font_size", "fontsize", "size"),
        "primary": ("primary", "primary_colour", "primary_color", "color"),
        "secondary": (
            "secondary",
            "secondary_colour",
            "secondary_color",
        ),
        "outline_color": ("outline_color", "outline_colour"),
        "back_color": ("back_color", "back_colour", "background"),
        "bold": ("bold",),
        "italic": ("italic",),
        "underline": ("underline",),
        "strikeout": ("strikeout", "strike_out"),
        "scale_x": ("scale_x", "scalex"),
        "scale_y": ("scale_y", "scaley"),
        "spacing": ("spacing",),
        "angle": ("angle",),
        "border_style": ("border_style", "borderstyle"),
        "outline": ("outline",),
        "shadow": ("shadow",),
        "alignment": ("alignment", "align"),
        "margin_l": ("margin_l", "marginl"),
        "margin_r": ("margin_r", "marginr"),
        "margin_v": ("margin_v", "marginv"),
        "encoding": ("encoding",),
    }
    for target, keys in mapping.items():
        value = _style_value(style, *keys)
        if value is not None:
            merged[target] = value

    merged["name"] = str(merged["name"])
    merged["font"] = str(merged["font"])
    for key in ("bold", "italic", "underline", "strikeout"):
        merged[key] = _flag(merged[key])
    for key in ("primary", "secondary", "outline_color", "back_color"):
        merged[key] = _ass_color(merged[key])
    return merged


def _normalise_styles(styles):
    if styles is None:
        styles = []
    if isinstance(styles, dict):
        styles = [dict(value, name=key) if isinstance(value, dict) else {"name": key}
                  for key, value in styles.items()]

    normalised = [_normalise_style(style) for style in styles]
    if not any(style["name"] == "Default" for style in normalised):
        normalised.insert(0, dict(_DEFAULT_STYLE))
    return normalised


def _style_line(style):
    return "Style: " + ",".join(
        [
            str(style["name"]),
            _quote_font(style["font"]),
            str(int(style["font_size"])),
            str(style["primary"]),
            str(style["secondary"]),
            str(style["outline_color"]),
            str(style["back_color"]),
            str(style["bold"]),
            str(style["italic"]),
            str(style["underline"]),
            str(style["strikeout"]),
            str(int(style["scale_x"])),
            str(int(style["scale_y"])),
            str(int(style["spacing"])),
            str(int(style["angle"])),
            str(int(style["border_style"])),
            str(style["outline"]),
            str(style["shadow"]),
            str(int(style["alignment"])),
            str(int(style["margin_l"])),
            str(int(style["margin_r"])),
            str(int(style["margin_v"])),
            str(int(style["encoding"])),
        ]
    )


def _normalise_text(text):
    if text is None:
        return ""
    if isinstance(text, bytes):
        text = decode_subtitle_bytes(text)
    text = normalize_newlines(str(text))
    return text.replace(_BOM, "")


def build_ass(subtitles, styles=None, play_res=(1920, 1080), title="Subtitles"):
    """Build a complete ASS document for ``subtitles``.

    ``subtitles`` is a list of dicts with ``start``/``end`` (seconds), an
    optional ``text``, ``layer``, ``style`` and margin/name/effect fields.
    ``styles`` may be a list of style dicts or a mapping of name -> style.
    Font names containing spaces are quoted so the comma separated ``Style:``
    line remains parseable.  SRT inline tags are converted to ASS overrides
    and long lines are wrapped inside the horizontal safe area.
    """

    try:
        res_x, res_y = int(play_res[0]), int(play_res[1])
    except (TypeError, ValueError, IndexError):
        res_x, res_y = 1920, 1080

    normalised_styles = _normalise_styles(styles)
    style_by_name = {style["name"]: style for style in normalised_styles}
    default_style = style_by_name.get("Default", _DEFAULT_STYLE)

    cues = resolve_overlaps(subtitles or [])

    header = [
        "[Script Info]",
        "; Generated by subtitle.ass",
        f"Title: {title}",
        "ScriptType: v4.00+",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: TV.709",
        f"PlayResX: {res_x}",
        f"PlayResY: {res_y}",
        "",
        "[V4+ Styles]",
        "Format: " + ", ".join(_STYLE_FIELDS),
    ]
    header.extend(_style_line(style) for style in normalised_styles)
    header.extend(["", "[Events]", _EVENT_FORMAT])

    events = []
    for cue in cues:
        start = float(cue.get("start", 0.0))
        end = float(cue.get("end", start))
        if end < start:
            end = start + signed_duration(start, end)

        style_name = str(cue.get("style") or "Default")
        style = style_by_name.get(style_name, default_style)

        text = _normalise_text(cue.get("text", ""))
        ass_text = srt_tags_to_ass(text)
        ass_text = wrap_ass_text(
            ass_text,
            play_res=(res_x, res_y),
            font_size=style.get("font_size", 48),
        )

        layer = int(cue.get("layer", 0) or 0)
        name = str(cue.get("name", "") or "")
        margin_l = int(cue.get("margin_l", 0) or 0)
        margin_r = int(cue.get("margin_r", 0) or 0)
        margin_v = int(cue.get("margin_v", 0) or 0)
        effect = str(cue.get("effect", "") or "")

        events.append(
            f"Dialogue: {layer},{format_ass_time(start)},{format_ass_time(end)},"
            f"{style_name},{name},{margin_l},{margin_r},{margin_v},{effect},"
            f"{ass_text}"
        )

    return "\n".join(header + events) + "\n"


# ---------------------------------------------------------------------------
# SRT input handling (BOM, encoding and line endings)
# ---------------------------------------------------------------------------


def normalize_newlines(text):
    """Convert CRLF/CR line endings to LF and drop a leading BOM."""

    if isinstance(text, bytes):
        text = decode_subtitle_bytes(text)
    return str(text).replace(_BOM, "").replace("\r\n", "\n").replace("\r", "\n")


def decode_subtitle_bytes(data, encoding=None):
    """Decode subtitle bytes, honouring a BOM and common legacy encodings."""

    if isinstance(data, str):
        return data.replace(_BOM, "")

    if not isinstance(data, (bytes, bytearray)):
        raise TypeError(f"expected bytes, got {type(data).__name__}")

    data = bytes(data)

    if encoding:
        return data.decode(encoding).replace(_BOM, "")

    if data.startswith(codecs.BOM_UTF8):
        return data.decode("utf-8-sig").replace(_BOM, "")
    if data.startswith(codecs.BOM_UTF32_LE) or data.startswith(codecs.BOM_UTF32_BE):
        # Check UTF-32 before UTF-16: the LE BOM starts with the UTF-16 LE BOM.
        return data.decode("utf-32").replace(_BOM, "")
    if data.startswith(codecs.BOM_UTF16_LE) or data.startswith(codecs.BOM_UTF16_BE):
        return data.decode("utf-16").replace(_BOM, "")

    # utf-8 first, then encodings common in Chinese/Japanese subtitle files.
    for candidate in ("utf-8", "gb18030", "big5", "shift_jis", "latin-1"):
        try:
            return data.decode(candidate).replace(_BOM, "")
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace").replace(_BOM, "")


def read_subtitle_file(path, encoding=None):
    """Read a subtitle file, handling BOM, legacy encodings and line endings."""

    with open(path, "rb") as handle:
        return normalize_newlines(decode_subtitle_bytes(handle.read(), encoding))


def parse_srt(text):
    """Parse SRT text into ``{start, end, text, layer}`` cue dicts.

    A BOM at the start of the file and CRLF/CR line endings are handled, so
    the first timecode is not lost when a UTF-8 BOM is present.
    """

    text = normalize_newlines(text)
    blocks = re.split(r"\n[ \t]*\n", text)

    cues = []
    for block in blocks:
        lines = [line for line in block.split("\n") if line.strip()]
        if not lines:
            continue

        time_index = next(
            (i for i, line in enumerate(lines) if "-->" in line), None
        )
        if time_index is None:
            continue

        time_line = lines[time_index]
        left, _, right = time_line.partition("-->")
        try:
            start = parse_srt_time(left)
            # The end side may carry SRT position coordinates after the time.
            end_token = right.strip().split()[0] if right.strip() else ""
            end = parse_srt_time(end_token)
        except (ValueError, IndexError):
            continue

        cue_text = "\n".join(lines[time_index + 1 :]).strip("﻿ \t")
        # A cue that runs past midnight has end < start; store it as an
        # explicit signed duration so retiming and export agree on its length.
        duration = signed_duration(start, end)
        cues.append(
            {
                "start": start,
                "end": start + duration,
                "text": cue_text,
                "layer": 0,
            }
        )

    return cues


def srt_to_ass(
    srt_text,
    source_fps=None,
    target_fps=None,
    styles=None,
    play_res=(1920, 1080),
):
    """Convert SRT text (or bytes) straight to an ASS document.

    ``source_fps``/``target_fps`` are optional; when both are given the cues
    are retimed on the frame grid before export.
    """

    cues = parse_srt(srt_text)
    if source_fps is not None and target_fps is not None and source_fps != target_fps:
        cues = convert_fps(cues, source_fps, target_fps)
    return build_ass(cues, styles=styles, play_res=play_res)
