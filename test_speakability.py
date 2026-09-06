"""Golden corpus for ~/.claude/tts/speakability.py (Phase A spec §A1.7).

Style contract copied from ~/.claude/hooks/test_speak_summary.py: plain
pytest functions, stdlib + pytest only, runnable via

    python3 -m pytest ~/.claude/tts/test_speakability.py -q

The module under test is loaded by ABSOLUTE PATH so the suite works from any
cwd and under any interpreter.  Point SPEAKABILITY_PATH at a mutated copy to
run the §A1.7 mutation table:

    SPEAKABILITY_PATH=/tmp/mut/speakability.py python3 -m pytest \
        ~/.claude/tts/test_speakability.py -q

54 cases, all required: 37 table-driven corpus rows (G01-G37) + 17 named
tests (G38-G54).  The "corpus" referred to by the G43 / G52 / G53 property
tests is exactly the module-level CORPUS list below.
"""
import importlib.util
import os
import re

import pytest

SPK_PATH = os.environ.get("SPEAKABILITY_PATH") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "speakability.py")

_LOADED = []


def spk():
    """Load the module under test lazily, so a MISSING module fails every
    test individually (54 identical failures) instead of erroring collection."""
    if not _LOADED:
        spec = importlib.util.spec_from_file_location(
            "speakability_under_test", SPK_PATH)
        if spec is None or spec.loader is None:
            raise ModuleNotFoundError(SPK_PATH)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        # the corpus owns its codename table so results do not depend on glossary files
        m.CODENAMES.clear()
        m.CODENAMES.update({"NIMBUS": "Nimbus", "ORBITAL": "Orbital", "FALCON": "Falcon"})
        _LOADED.append(m)
    return _LOADED[0]


# Same class the module uses; the test owns its own copy on purpose so a
# mutation that guts the module's emoji rule cannot also guts the assertion.
_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"  # emoticons, pictographs, transport, symbols-ext
    "\u2600-\u27BF"          # misc symbols + dingbats
    "\u2B00-\u2BFF"          # misc symbols and arrows
    "\uFE0E\uFE0F"           # variation selectors
    "\u200D"                  # zero-width joiner
    "\u20E3"                  # combining enclosing keycap
    "]+"
)

# --------------------------------------------------------------- the corpus
# Each row: (id, [(input, [(kind, arg), ...]), ...])
# kinds: "in" / "notin" / "eq" / "no_emoji"

G20_TABLE = ("Results:\n| name | score |\n|---|---|\n| alpha | 1 |\n"
             "| beta | 2 |\nDone.")
G21_BULLETS = ("Plan:\n- first item\n- second item\n• third thing\n"
               "1. fourth step")
G31_FENCE = "Done. Results below.\n```python\nsecret = 1\nmore leaked lines"

CORPUS = [
    ("G01", [("Edited /home/alice/project/pkg/judge.py now",
              [("in", "judge.py"), ("notin", "/home"), ("notin", "pkg/")])]),
    ("G02", [("check ~/notes.md later",
              [("in", "home notes.md")])]),
    ("G03", [("wrote /mnt/c/Users/alice/AppData/Local/Temp/kokoro-tts-x.wav",
              [("in", "kokoro-tts-x.wav"), ("notin", "mnt")])]),
    ("G04", [("deployed commit 3f9c2ab7de4451aa90ff to prod",
              [("in", "hash 3f9c2a"), ("notin", "3f9c2ab7de4451aa90ff")])]),
    ("G05", [("master=91037fc is the merge point",
              [("in", "hash 9103"), ("notin", "91037fc")])]),
    ("G06", [("commit 4ffb90a landed",
              [("in", "commit hash 4ffb")])]),
    ("G07", [("the widget id 4ffb90a shows up twice",
              [("in", "4ffb90a")])]),
    ("G08", [("rev acceded to the change",
              [("in", "acceded")])]),
    ("G09", [("shipped 2026-07-28 on time",
              [("in", "July 28, 2026"), ("notin", "2026-07-28")])]),
    ("G10", [("batch 2 sent 07-26 from Proton",
              [("in", "July 26")])]),
    ("G11", [("kokoro-v1.0.onnx with codex-cli 0.144.6",
              [("notin", "July"), ("in", "0.144.6"), ("in", "onyx")])]),
    ("G12", [("blind A/B Sonnet 3-0 confirmed",
              [("notin", "March"), ("in", "3-0")])]),
    ("G13", [("pane 10-12 rebooted",
              [("notin", "October"), ("in", "10-12")])]),
    ("G14", [("raised $462M at a $3.5B cap",
              [("in", "462 million dollars"), ("in", "3.5 billion dollars"),
               ("notin", "$")])]),
    ("G15", [("three paid $149 orders",
              [("in", "149 dollars orders")])]),
    ("G16", [("grew 3.5x this month",
              [("in", "3.5 times")])]),
    ("G17", [("recon → FLEET in one day",
              [("in", " to "), ("notin", "→")])]),
    ("G18", [("winplayer -> one-shot powershell",
              [("in", "winplayer to one-shot"), ("notin", "->")])]),
    ("G19", [("Done.\n---\nNext up.",
              [("notin", "---"), ("in", "Done. Next up.")])]),
    ("G20", [(G20_TABLE,
              [("in", "a table of 2 rows: alpha, beta"), ("notin", "|")])]),
    ("G21", [(G21_BULLETS,
              [("in", "first item. second item. third thing. fourth step.")])]),
    ("G22", [("Falcon · fleet live · Orbital next",
              [("notin", "·"), ("in", "Falcon, fleet live")])]),
    ("G23", [("PayGrid and MockBank are live",
              [("in", "Pay Grid"), ("in", "Mock Bank")])]),
    ("G24", [("pushed to GitHub via PowerShell",
              [("in", "GitHub"), ("notin", "Git Hub"), ("in", "PowerShell")])]),
    ("G25", [("NIMBUS OS is live",
              [("in", "Nimbus"), ("notin", "NIMBUS")])]),
    ("G26", [("NIMBUS's console shipped",
              [("in", "Nimbus's")])]),
    ("G27", [("ORBITAL and FALCON shipped today",
              [("in", "Orbital"), ("in", "Falcon")])]),
    ("G28", [("runs under WSL2 with fcntl locks",
              [("in", "W S L two"), ("in", "F control")])]),
    ("G29", [("the tmux pane died",
              [("in", "tee mux")])]),
    ("G30", [("Deploy complete 🚀🔥 next step ⚠️ check ✅",
              [("no_emoji", None), ("in", "Deploy complete next step check")])]),
    ("G31", [(G31_FENCE,
              [("notin", "secret"), ("notin", "leaked"), ("in", "Done.")])]),
    ("G32", [("see [[nimbus-checklist|the checklist]] and [[falcon-blueprint]]",
              [("in", "the checklist"), ("in", "falcon-blueprint"),
               ("notin", "nimbus-checklist")])]),
    ("G33", [("docs at https://example.com/x are stale",
              [("in", " link "), ("notin", "https")])]),
    ("G34", [("code abc123 ok", [("in", "abc123")]),
             ("id 1234567890123456 ok", [("in", "1234567890123456")])]),
    ("G35", [("register 0x1F is set",
              [("notin", "times")])]),
    ("G36", [("Merged the branch. Ran the suite.",
              [("eq", "Merged the branch. Ran the suite.")])]),
    ("G37", [("", [("eq", "")]),
             ("   ", [("eq", "")])]),
]

CORPUS_INPUTS = [inp for _id, subs in CORPUS for inp, _checks in subs]


def _check(out, kind, arg, cid, inp):
    ctx = "%s input=%r out=%r" % (cid, inp, out)
    if kind == "in":
        assert arg in out, "%s: expected %r IN output" % (ctx, arg)
    elif kind == "notin":
        assert arg not in out, "%s: expected %r NOT IN output" % (ctx, arg)
    elif kind == "eq":
        assert out == arg, "%s: expected output == %r" % (ctx, arg)
    elif kind == "no_emoji":
        assert _EMOJI_RE.search(out) is None, "%s: emoji survived" % ctx
    else:  # pragma: no cover - guard against a typo in the table
        raise AssertionError("unknown check kind %r" % (kind,))


@pytest.mark.parametrize("cid", [c[0] for c in CORPUS])
def test_corpus(cid):
    row = dict(CORPUS)[cid]
    m = spk()
    for inp, checks in row:
        out = m.speakable(inp)
        for kind, arg in checks:
            _check(out, kind, arg, cid, inp)


# ---------------------------------------------------------- named tests
# G38-G54.  Not table-driven: each pins one specific contract.

def test_g38_truncate_never_cuts_a_word():
    m = spk()
    src = "The quick brown fox jumps over the lazy dog repeatedly."
    out = m.truncate_speakable(src, 40)
    assert len(out) <= 40, out
    assert out.endswith("."), out
    for w in out.rstrip(".").split():
        assert w in src.split(), "partial word %r in %r" % (w, out)


def test_g39_dangling_connective_is_dropped():
    m = spk()
    # budget 30 is load-bearing: the raw cut is "alpha beta gamma delta and",
    # so the _DANGLING branch is the only thing that removes "and".
    out = m.truncate_speakable("alpha beta gamma delta and epsilon zeta", 30)
    assert out == "alpha beta gamma delta.", out


def test_g40_whole_sentence_preference():
    m = spk()
    # budget 20 is load-bearing: the input is 54 chars, so the
    # len(text) <= budget short-circuit does NOT fire.
    src = "Short one. A considerably longer second sentence here."
    assert len(src) > 20
    assert m.truncate_speakable(src, 20) == "Short one."


def test_g41_sentences_abbreviation_safe():
    m = spk()
    assert len(m.sentences("Dr. Smith merged it. Next up.")) == 2


def test_g42_sentences_eg_is_not_a_boundary():
    m = spk()
    assert len(m.sentences("Use a queue, e.g. a ring buffer. Then measure.")) == 2


def test_g43_idempotent_and_no_artifacts():
    m = spk()
    for x in CORPUS_INPUTS:
        once = m.speakable(x)
        assert m.speakable(once) == once, "not idempotent on %r -> %r" % (x, once)
        for bad in ("```", "|", "[[", "→", "$", "/mnt/", "~/"):
            assert bad not in once, "artifact %r survived %r -> %r" % (bad, x, once)
        assert _EMOJI_RE.search(once) is None, "emoji survived %r -> %r" % (x, once)


def test_g44_numeric_dash_range():
    m = spk()
    out = m.speakable("scaled 2–3 nodes overnight")
    assert "2 to 3" in out, out
    assert "–" not in out, out


def test_g45_equals_is_residue_and_allcaps_snake_splits():
    m = spk()
    out = m.speakable("set KOKORO_CHUNKER=v2 and rerun")
    assert "=" not in out, out
    assert "KOKORO CHUNKER" in out, out


def test_g46_snake_case_filename_keeps_its_underscore():
    m = spk()
    out = m.speakable("Edited /home/a/b/test_judge.py after the fix")
    assert "test_judge.py" in out, out
    assert "test judge.py" not in out, out


def test_g47_no_whitespace_fallback():
    m = spk()
    out = m.truncate_speakable("a" * 80, 20)
    assert len(out) <= 20, out
    assert out.endswith("."), out


def test_g48_trailing_junk_is_removed_not_decorated():
    m = spk()
    out = m.truncate_speakable(
        "Ready for review, then we ship it out tomorrow morning", 20)
    assert len(out) <= 20, out
    assert ",." not in out, out
    assert ";." not in out, out


def test_g49_phrase_runs_stage_1a():
    m = spk()
    out = m.phrase("build failed, see https://ci.example.com/job/42 for the log")
    assert "link" in out, out
    assert "https" not in out, out
    out2 = m.phrase("done ```secret = 1``` ok")
    assert "secret" not in out2, out2
    assert "code block" in out2, out2


def test_g50_truncate_rule_ordering():
    m = spk()
    assert m.truncate_speakable("x", 1) == "x"
    assert m.truncate_speakable("hello world", 1) == ""


G51_INPUT = ("Merged ~/notes.md and /home/a/b/test_judge.py 🚀 "
             "see https://ci.example.com/job/42 — NIMBUS ships 2026-07-28, "
             "PayGrid took $149, hash 3f9c2ab7de4451aa90ff, WSL2 only")


def test_g51_transport_safe_is_conservative():
    m = spk()
    out = m.transport_safe(G51_INPUT)
    # removed by the subset
    assert " link " in out, out
    assert "https" not in out, out
    assert "/home/" not in out, out
    assert "test_judge.py" in out, out
    assert "home notes.md" in out, out
    assert _EMOJI_RE.search(out) is None, out
    assert "hash 3f9c2a" in out, out
    assert "3f9c2ab7de4451aa90ff" not in out, out
    # PRESERVED — this is the point of the case: a transport_safe that quietly
    # ran the producer-quality rules fails here.
    assert "NIMBUS" in out, out
    assert "2026-07-28" in out, out
    assert "$149" in out, out
    assert "PayGrid" in out, out
    assert "WSL2" in out, out
    assert "—" in out, out


def test_g52_transport_safe_is_a_fixed_point_on_producer_output():
    """Hard invariant 9 — the transport must be a no-op on producer output,
    or ~/.claude/tts-spoken.log is a lie."""
    m = spk()
    for x in CORPUS_INPUTS:
        s = m.speakable(x)
        assert m.transport_safe(s) == s, "speakable %r -> %r changed" % (x, s)
        p = m.phrase(x)
        assert m.transport_safe(p) == p, "phrase %r -> %r changed" % (x, p)


def test_g53_transport_safe_is_idempotent():
    m = spk()
    for x in CORPUS_INPUTS:
        once = m.transport_safe(x)
        assert m.transport_safe(once) == once, "not idempotent on %r" % (x,)
        assert "\r" not in once, once


CLEAN_4000 = ("The migration finished and the console is live. " * 100)[:4000]


def test_g54_transport_safe_never_truncates():
    m = spk()
    assert m.transport_safe("") == ""
    assert m.transport_safe("   ") == ""
    assert len(CLEAN_4000) == 4000
    assert m.transport_safe(CLEAN_4000) == CLEAN_4000
