"""Markdown / terminal text -> text a TTS engine reads well.

Single source of truth for the voice rig's three speech producers
(`~/.claude/hooks/speak-summary.py`, `voicefleet/judge.py`,
`voicefleet/audio.py::Speaker.speak`) plus the transport choke point
(`~/.claude/tts/kokoro_say.py`).  Phase A spec §A1.

Pure functions, STDLIB ONLY (`re` and nothing else): no I/O, no env reads,
no logging, no global mutable state.  That purity is what makes it
usable from three different interpreters and trivially testable.

Golden corpus: ~/.claude/tts/test_speakability.py (54 cases).
"""
import re

__version__ = "1.0.0"          # bumped on every table/regex change

# ------------------------------------------------------------------ tables

_MONTHS = ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December")

# ALL-CAPS project codenames -> spoken form. Matched as a whole token, with an
# optional possessive suffix ('s or ’s) that is preserved. Filled from
# glossary.json (shipped examples) and glossary.local.json (yours, gitignored)
# next to this file; both optional, both {"codenames": {...}, "glossary": {...}}.
CODENAMES = {}


def _load_glossary_files():
    """The module's one I/O: read the two glossary files at import (json is stdlib)."""
    import json as _json
    import os as _os
    here = _os.path.dirname(_os.path.abspath(__file__))
    for name in ("glossary.json", "glossary.local.json"):
        try:
            with open(_os.path.join(here, name), encoding="utf-8") as f:
                d = _json.load(f)
        except Exception:
            continue
        CODENAMES.update({str(k).upper(): str(v) for k, v in (d.get("codenames") or {}).items()})
        GLOSSARY.update({str(k): str(v) for k, v in (d.get("glossary") or {}).items()})

# CamelCase tokens that must NOT be split at the lower->upper boundary.
_CAMEL_KEEP = frozenset((
    "GitHub", "GitLab", "JavaScript", "TypeScript", "PowerShell", "YouTube",
    "LinkedIn", "PayPal", "OpenAI", "ChatGPT", "iPhone", "iPad", "macOS",
    "iOS", "DevOps", "PostgreSQL", "MySQL", "SQLite", "NumPy", "PyTorch",
    "TensorFlow", "JSONL", "McpTransport",
))

# Whole-token jargon -> spoken form (case-SENSITIVE, longest key first).
GLOSSARY = {
    "WSL2": "W S L two", "WSL": "W S L", "PCM_16": "P C M sixteen",
    "PCM16": "P C M sixteen", "fcntl": "F control", "tmux": "tee mux",
    "pytest": "pie test", "jsonl": "jason L", "stdin": "standard in",
    "stdout": "standard out", "stderr": "standard error", "onnx": "onyx",
    "venv": "V env", "npm": "N P M", "sudo": "soo doo", "cwd": "C W D",
    "PHI": "P H I",
}
# NOTE: "wav" is deliberately NOT in GLOSSARY — it would mangle filenames
# ("kokoro-tts-x.wav" -> "...x.wave"). Do not add it.

_DANGLING = frozenset((
    "and", "or", "but", "the", "a", "an", "to", "of", "in", "on", "for",
    "with", "at", "by", "from", "that", "which", "is", "was", "as", "into",
))

# Trailing characters a word-boundary cut leaves behind that must be REMOVED
# (not decorated with a '.') by repair_terminal_punctuation.
_TRAILING_JUNK = ",;-–—"

# Money. NOTE the `(?:\s*([kKmMbB]))?` grouping — it differs from the shipped
# speak-summary `_MONEY_RE` and the difference is a BUG FIX: `\s*([kKmMbB])?\b`
# swallows the space after a suffix-less amount, so "$149 orders" became
# "149 dollarsorders". Making the whitespace part of the optional suffix group
# keeps the space.
_MONEY_RE = re.compile(r"\$(\d+(?:,\d{3})*(?:\.\d+)?)(?:\s*([kKmMbB]))?\b")

# Underscores. `test_judge.py` must survive; `_emphasis_` must not.
_SNAKE_RE = re.compile(r"[A-Za-z0-9]+(?:_[A-Za-z0-9]+)+")
_EDGE_US_RE = re.compile(r"(?<![A-Za-z0-9])_|_(?![A-Za-z0-9])")

_ABBREV = ("Dr", "Mr", "Mrs", "Ms", "Prof", "Sr", "Jr", "St", "vs", "etc",
           "approx", "Fig", "No", "Inc", "Ltd", "Co", "cf", "al", "Sept",
           "Jan", "Feb", "Mar", "Apr", "Jun", "Jul", "Aug", "Oct", "Nov", "Dec")

# ----------------------------------------------------------------- stage 1a

_FENCE_RE = re.compile(r"```.*?```", re.S)
_FENCE_TAIL_RE = re.compile(r"```.*", re.S)
_TICK_RE = re.compile(r"`[^`]*`")
_WIKILINK_ALIAS_RE = re.compile(r"\[\[([^\[\]|]*)\|([^\[\]|]*)\]\]")
_WIKILINK_RE = re.compile(r"\[\[([^\[\]|]*)\]\]")
_URL_RE = re.compile(r"https?://\S+")

# ----------------------------------------------------------------- stage 1b

# >=3 repeats of the SAME char and nothing else on the line, so a markdown
# table separator (`|---|---|`, which contains `|`) can never match.
_DIVIDER_RE = re.compile(r"^[ \t]*([-*_=])(?:[ \t]*\1){2,}[ \t]*$", re.M)
_BULLET_RE = re.compile(r"^(\s*)(?:[-*•·‣]|\d+[.)])\s+(.*\S)\s*$")

# ------------------------------------------------------------------ stage 2

# absolute (or ~-rooted) path with >=2 segments -> spoken as its basename
_PATH_RE = re.compile(r"(?<![\w.~])~?/(?:[\w.+@-]+/)+[\w.+@-]*[\w+@-]")
_HOME_RE = re.compile(r"(?<![\w.])~/")
# >=12-char hex run with at least one letter -> "hash <first-6>"
_HEX_RE = re.compile(r"\b(?=[0-9a-fA-F]*[a-fA-F])[0-9a-fA-F]{12,}\b")

_SHA_RE = re.compile(
    r"\b(?=[0-9a-f]{7,11}\b)(?=[0-9a-f]*[a-f])(?=[0-9a-f]*[0-9])[0-9a-f]{7,11}\b")
_SHA_CTX_RE = re.compile(
    r"(?i)(?:\b(?:commit|commits|committed|sha|sha1|hash|rev|revision|head|tag|"
    r"tagged|pushed|merged|master|branch|at|in)\b[^\n]{0,12}|[=:@]\s*)$")

_ISO_RE = re.compile(r"\b(20\d{2})-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])\b")

_MMDD_RE = re.compile(r"(?<![\w./-])(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])(?![\w./%-])")
_DATE_CUE_RE = re.compile(
    r"(?i)(?:\b(?:on|by|since|until|through|thru|shipped|sent|due|dated|updated|"
    r"upd|launched|scheduled|eta|deadline|effective|revisit|review-by|as of)\b"
    r"[^\n]{0,12}|~)$")

_DASH_RANGE_RE = re.compile(r"(?<=\d)\s*[–—]\s*(?=\d)")
_TIMES_RE = re.compile(r"\b(\d+(?:\.\d+)?)x\b")
_ARROW_TO_RE = re.compile(r"-{1,2}>|→|=>|⇒")
_ARROW_FROM_RE = re.compile(r"<-{1,2}|←")
_SEP_BULLET_RE = re.compile(r"[•·‣]")
_ENDASH_LOOSE_RE = re.compile(r"(?<![0-9])–|–(?![0-9])")
_CODENAME_RE = re.compile(r"\b([A-Z]{3,})(['’]s)?\b")
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*")
_CAMEL_SPLIT_RE = re.compile(r"(?<=[a-z])(?=[A-Z])")
_load_glossary_files()
_GLOSSARY_ORDER = sorted(GLOSSARY, key=len, reverse=True)
_GLOSSARY_RE = {k: re.compile(r"\b%s\b" % re.escape(k)) for k in GLOSSARY}
_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"  # emoticons, pictographs, transport, symbols-ext
    "\u2600-\u27BF"          # misc symbols + dingbats
    "\u2B00-\u2BFF"          # misc symbols and arrows
    "\uFE0E\uFE0F"           # variation selectors
    "\u200D"                  # zero-width joiner between emoji parts
    "\u20E3"                  # combining enclosing keycap
    "]+"
)
# Leftover markdown chars. `_` is owned by 2.15b; `~` stays excluded (it marks
# home-paths handled by 2.1/2.2).
_RESIDUE_RE = re.compile(r"[#*>|\[\]{}()=]")
_HASH_DEDUPE_RE = re.compile(r"(?i)\bhash\s+hash\b")

# ------------------------------------------------------------------ stage 3

_WS_RE = re.compile(r"\s+")
_SPACE_BEFORE_PUNCT_RE = re.compile(r"\s+([,.;:!?])")
_DOUBLE_SEP_RE = re.compile(r"([,;:])\s*(?=[,;:])")

# --------------------------------------------------------------- sentences()

_SPLIT_CANDIDATE_RE = re.compile(r'(?<=[.!?…])["\')\]]*\s+')
_ABBREV_TAIL_RE = re.compile(
    r"(?:\b(?:%s)|\b[A-Za-z]|\b(?:e\.g|i\.e|a\.m|p\.m|U\.S|U\.K))\.$"
    % "|".join(_ABBREV))
_NUM_TAIL_RE = re.compile(r"\d\.$")

_TERMINAL = ".!?…:"


# ============================================================ stage functions

def _stage0(text):
    """Normalize line endings. No Unicode NFKC: it would rewrite emoji/quote
    forms the later rules and the SAPI escaper depend on."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _stage1a(text):
    """Inline structure — runs in BOTH entry points and in transport_safe."""
    text = _FENCE_RE.sub(" code block ", text)
    text = _FENCE_TAIL_RE.sub(" code block ", text)
    text = _TICK_RE.sub(" ", text)
    text = _WIKILINK_ALIAS_RE.sub(r"\2", text)
    text = _WIKILINK_RE.sub(r"\1", text)
    text = _URL_RE.sub(" link ", text)
    return text


def _is_table_sep(line):
    return ("|" in line and "---" in line
            and re.fullmatch(r"[\s|:\-]+", line) is not None)


def _tables_to_speech(text):
    """Markdown tables -> 'a table of N rows: <first-column values>'."""
    lines = text.split("\n")
    out, i = [], 0
    while i < len(lines):
        if "|" not in lines[i]:
            out.append(lines[i])
            i += 1
            continue
        j = i
        while j < len(lines) and "|" in lines[j]:
            j += 1
        group = lines[i:j]
        i = j
        if not any(_is_table_sep(ln) for ln in group):
            out.extend(group)  # pipes but no separator: not a table
            continue
        sep_at = next(k for k, ln in enumerate(group) if _is_table_sep(ln))
        data = [ln for k, ln in enumerate(group)
                if not _is_table_sep(ln) and k != sep_at - 1]  # drop header row
        firsts = []
        for row in data:
            cells = [c.strip() for c in row.strip().strip("|").split("|")]
            if cells and cells[0]:
                firsts.append(cells[0])
        spoken = "a table of %d rows" % len(data)
        if firsts:
            spoken += ": " + ", ".join(firsts)
        out.append(spoken + ".")
    return "\n".join(out)


def _bullets_to_sentences(text):
    """Line-leading bullets become their own sentences so list items don't run
    together once whitespace collapses."""
    out = []
    for ln in text.split("\n"):
        mm = _BULLET_RE.match(ln)
        if mm:
            item = mm.group(2)
            if not re.search(r"[.!?:;]$", item):
                item += "."
            out.append(mm.group(1) + item)
        else:
            out.append(ln)
    return "\n".join(out)


def _stage1b(text):
    """Block structure — needs real newlines; speakable() ONLY."""
    text = _DIVIDER_RE.sub("", text)
    text = _tables_to_speech(text)
    text = _bullets_to_sentences(text)
    return text


# --- stage 2, one function per numbered rule (order is load-bearing) -------

def _r21_path(text):
    return _PATH_RE.sub(lambda m: m.group(0).rsplit("/", 1)[-1], text)


def _r22_home(text):
    return _HOME_RE.sub("home ", text)


def _r23_hex(text):
    return _HEX_RE.sub(lambda m: "hash " + m.group(0)[:6], text)


def _r24_short_sha(text):
    out, last = [], 0
    for m in _SHA_RE.finditer(text):
        ctx = text[max(0, m.start() - 24):m.start()]
        if not _SHA_CTX_RE.search(ctx):
            continue
        out.append(text[last:m.start()])
        out.append("hash " + m.group(0)[:4])
        last = m.end()
    out.append(text[last:])
    return _HASH_DEDUPE_RE.sub("hash", "".join(out))


def _r25_iso_date(text):
    def rep(m):
        return "%s %d, %s" % (_MONTHS[int(m.group(2)) - 1], int(m.group(3)),
                              m.group(1))
    return _ISO_RE.sub(rep, text)


def _r26_mmdd_date(text):
    out, last = [], 0
    for m in _MMDD_RE.finditer(text):
        ctx = text[max(0, m.start() - 20):m.start()]
        if not _DATE_CUE_RE.search(ctx):
            continue
        out.append(text[last:m.start()])
        out.append("%s %d" % (_MONTHS[int(m.group(1)) - 1], int(m.group(2))))
        last = m.end()
    out.append(text[last:])
    return "".join(out)


def _r27_dash_range(text):
    return _DASH_RANGE_RE.sub(" to ", text)


def _money_words(m):
    suffix = {"k": " thousand", "m": " million", "b": " billion"}.get(
        (m.group(2) or "").lower(), "")
    return m.group(1) + suffix + " dollars"


def _r28_money(text):
    return _MONEY_RE.sub(_money_words, text)


def _r29_times(text):
    return _TIMES_RE.sub(r"\1 times", text)


def _r210_arrows(text):
    text = _ARROW_TO_RE.sub(" to ", text)
    text = _ARROW_FROM_RE.sub(" from ", text)
    return text


def _r211_separators(text):
    text = _SEP_BULLET_RE.sub(", ", text)
    text = text.replace("—", ", ")
    text = _ENDASH_LOOSE_RE.sub(", ", text)
    return text


def _r212_codenames(text):
    def rep(m):
        spoken = CODENAMES.get(m.group(1))
        if spoken is None:
            return m.group(0)
        return spoken + (m.group(2) or "")
    return _CODENAME_RE.sub(rep, text)


def _r213_camel(text):
    def rep(m):
        tok = m.group(0)
        if tok in _CAMEL_KEEP:
            return tok
        return _CAMEL_SPLIT_RE.sub(" ", tok)
    return _TOKEN_RE.sub(rep, text)


def _r214_glossary(text):
    for k in _GLOSSARY_ORDER:
        v = GLOSSARY[k]
        text = _GLOSSARY_RE[k].sub(lambda m, v=v: v, text)
    return text


def _r215_emoji(text):
    return _EMOJI_RE.sub(" ", text)


def _r215b_underscores(text):
    text = _SNAKE_RE.sub(
        lambda m: (m.group(0) if any(c.islower() for c in m.group(0))
                   else m.group(0).replace("_", " ")), text)
    return _EDGE_US_RE.sub(" ", text)


def _r216_residue(text):
    return _RESIDUE_RE.sub(" ", text)


def _stage2(text):
    text = _r21_path(text)
    text = _r22_home(text)
    text = _r23_hex(text)
    text = _r24_short_sha(text)
    text = _r25_iso_date(text)
    text = _r26_mmdd_date(text)
    text = _r27_dash_range(text)
    text = _r28_money(text)
    text = _r29_times(text)
    text = _r210_arrows(text)
    text = _r211_separators(text)
    text = _r212_codenames(text)
    text = _r213_camel(text)
    text = _r214_glossary(text)
    text = _r215_emoji(text)
    text = _r215b_underscores(text)
    text = _r216_residue(text)
    return text


def _stage3(text):
    text = _WS_RE.sub(" ", text).strip()
    text = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", text)
    text = _DOUBLE_SEP_RE.sub("", text)
    return text


# ================================================================ public API

def speakable(text, budget=None):
    """Full pipeline for multi-line markdown/terminal prose (the speak-summary
    lane). budget=None means no truncation. Empty/whitespace-only input
    returns ''. Idempotent."""
    if not text:
        return ""
    t = _stage0(text)
    t = _stage1a(t)
    t = _stage1b(t)
    t = _stage2(t)
    t = _stage3(t)
    return truncate_speakable(t, budget)


def phrase(text, budget=None):
    """Single-line pipeline for already-composed one-liners (voicefleet judge
    lines, arbiter digests). Identical to speakable() MINUS the *block* stage
    (1b), because a 20-word status line has no block structure and the bullet
    rule would mis-fire on a leading hyphen. It DOES run stage 1a. Idempotent.
    Never appends terminal punctuation unless it truncated."""
    if not text:
        return ""
    t = _stage0(text)
    t = _stage1a(t)
    t = _stage2(t)
    t = _stage3(t)
    return truncate_speakable(t, budget)


def sentences(text):
    """Abbreviation-safe sentence split. No truncation, no rewriting."""
    text = _WS_RE.sub(" ", text or "").strip()
    if not text:
        return []
    out, start = [], 0
    for m in _SPLIT_CANDIDATE_RE.finditer(text):
        head = text[start:m.start()]
        if _ABBREV_TAIL_RE.search(head):
            continue
        if (_NUM_TAIL_RE.search(head) and m.end() < len(text)
                and (text[m.end()].islower() or text[m.end()].isdigit())):
            continue
        h = head.strip()
        if h:
            out.append(h)
        start = m.end()
    tail = text[start:].strip()
    if tail:
        out.append(tail)
    return out


def repair_terminal_punctuation(text):
    """Make text end in speakable terminal punctuation. Never doubles
    punctuation. Idempotent."""
    if not text or not text.strip():
        return text
    out = text.rstrip()
    while out and out[-1] in _TRAILING_JUNK:
        out = out[:-1].rstrip()
    if not out:
        return out
    if out[-1] in _TERMINAL:
        return out
    return out + "."


def truncate_speakable(text, budget):
    """Budget-respecting truncation that never cuts inside a word and never
    leaves a fragment without terminal punctuation. Rules IN THIS ORDER."""
    if budget is None:                     # 1
        return text
    if len(text) <= budget:                # 2
        return text
    if budget < 2:                         # 3
        return ""
    picked, ln = [], 0                     # 4
    for s in sentences(text):
        add = len(s) if not picked else len(s) + 1
        if ln + add > budget:
            break
        picked.append(s)
        ln += add
    if picked:
        return " ".join(picked)
    cut = text.rfind(" ", 0, budget - 1)   # 5
    head = text[:budget - 1] if cut == -1 else text[:cut]
    head = head.strip()                    # 6
    words = head.split()
    if words and words[-1].lower() in _DANGLING:
        head = " ".join(words[:-1])
    return repair_terminal_punctuation(head)


def transport_safe(text):
    """Conservative subset for the TRANSPORT choke point (kokoro_say.py).

    stage 0 -> stage 1a -> 2.1 -> 2.2 -> 2.3 -> 2.15 -> stage 3.

    Never truncates (no budget parameter — deliberately). Never raises.
    Idempotent. MUST be a fixed point on any speakable()/phrase() output
    (hard invariant 9, pinned by G52).

    Every producer-quality rule (short-SHA, dates, dash ranges, money,
    multipliers, arrows, separators, codenames, CamelCase, glossary,
    underscores, markdown residue) and ALL of stage 1b are deliberately
    EXCLUDED: a producer that stopped normalizing must stay audibly wrong,
    and ~/.claude/tts-spoken.log must not lie about what was spoken.
    """
    try:
        if not text:
            return text if isinstance(text, str) else ""
        t = _stage0(text)
        t = _stage1a(t)
        t = _r21_path(t)
        t = _r22_home(t)
        t = _r23_hex(t)
        t = _r215_emoji(t)
        return _stage3(t)
    except Exception:
        return text
