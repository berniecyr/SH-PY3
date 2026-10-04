#! /usr/bin/env python
#*****************************************************************************
#
# testNudityMerge.py
#     Regression check for WHICH nudity classes reach the search panel.
#
#*****************************************************************************

"""Check that the detection panel shows every nudity class stored on a clip.

WHY THIS EXISTS
On 2026-09-07 a clip on the z_test camera appeared to miss
MALE_GENITALIA_EXPOSED while a later loop pass of the same footage showed it.
The detector was right both times -- BackEndApp.log stores the class on both
objects:

  08:56:50  dbId 146920  (F, ~30, faceDet 0.48)  MALE_BREAST_EXPOSED=0.74
  08:56:50  dbId 146922  (M, ~28, faceDet 0.75)  MALE_BREAST_EXPOSED=0.77,
                                                 MALE_GENITALIA_EXPOSED=0.30

Both objects belong to ONE clip ("Objects: 2 persons").  SearchDetectionPanel
latched the FIRST object's detail string and ignored every later object:

    if attr['nudityDetail'] and not nudityDetail:
        nudityDetail = attr['nudityDetail']

so the panel rendered 146920's 'MALE_BREAST_EXPOSED (0.74)' and threw away
146922's genitalia class entirely.  The 08:59 clip rendered correctly only by
luck: its earlier object (146971) had nudity=False, so the guard skipped it and
the genitalia-bearing object happened to be first.

That is the whole defect -- a clip with two people shows one person's classes,
and which person wins is an accident of object ordering.

METHOD NOTES (HANDOFF section 8)
  * "A test that never saw red proves nothing."  The checks that matter run the
    LEGACY first-wins behaviour alongside the fixed one in the same process and
    assert they differ in the measured direction, so they cannot pass against
    unfixed code.
  * "Don't anchor a regression check to live data."  The fixtures are the
    literal strings from the 2026-09-07 log quoted above, frozen into this file.
    Nothing here reads objdb2, clipdb, a log, or imagecheck_config.json, so it
    cannot rot when the disk cleaner runs or the databases are reset.
  * Importing frontEnd.SearchDetectionPanel is safe and fast (~0.4 s).  It pulls
    wx but constructs no window, and the two helpers under test are module-level
    pure functions precisely so this harness need not build a wx.App.

USAGE
    venv\\Scripts\\python.exe scripts\\testNudityMerge.py [-v]

Exit codes: 0 pass, 1 fail, 2 skipped (module unimportable), 3 bad invocation.
Nothing is read from or written to any database, log, or config file.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# The two clips from the 2026-09-07 z_test loop, exactly as stored.  Objects are
# listed in objList order; an object with nudity=False contributes nothing and
# is simply absent here, which is what the caller's `if attr['nudity']` does.
_kClip0856 = ["MALE_BREAST_EXPOSED=0.74",                           # 146920
              "MALE_BREAST_EXPOSED=0.77,MALE_GENITALIA_EXPOSED=0.30"]  # 146922
_kClip0859 = ["MALE_BREAST_EXPOSED=0.75,MALE_GENITALIA_EXPOSED=0.44"]  # 146976


def _legacyRender(details):
    """The pre-fix behaviour: first non-empty detail wins, rendered verbatim.

    This is the red arm.  It is a faithful copy of the code that shipped, kept
    here so every check below can be shown to fail against it.
    """
    nudityDetail = None
    for detail in details:
        if detail and not nudityDetail:
            nudityDetail = detail
    if not nudityDetail:
        return "Nudity detected"
    parts = []
    for p in nudityDetail.split(","):
        if "=" in p:
            cls, score = p.split("=", 1)
            parts.append("%s (%s)" % (cls, score))
        else:
            parts.append(p)
    return ", ".join(parts)


def _run(merge, fmt, verbose):
    failures = []

    def check(name, ok, detail=""):
        if ok:
            if verbose:
                print("  PASS  %s %s" % (name, detail))
        else:
            failures.append("%s %s" % (name, detail))
            print("  FAIL  %s %s" % (name, detail))

    def render(details):
        scores, extras = {}, []
        for d in details:
            merge(d, scores, extras)
        return fmt(scores, extras) or "Nudity detected"

    # ---------------------------------------------------------------- check 1
    # The reported defect: a second object's classes must survive.
    # RED ARM: legacy must drop the genitalia class on this exact fixture.
    fixed = render(_kClip0856)
    red = _legacyRender(_kClip0856)
    check("multi-object-classes-merged",
          "MALE_GENITALIA_EXPOSED" in fixed, "(got %r)" % fixed)
    check("multi-object-classes-merged-RED",
          "MALE_GENITALIA_EXPOSED" not in red,
          "(legacy got %r; must omit it or the fixture is wrong)" % red)

    # ---------------------------------------------------------------- check 2
    # Across objects the higher score wins: 0.77 from 146922, not 0.74 from
    # 146920.  RED ARM: legacy shows 0.74, the first object's value.
    check("multi-object-best-score-wins",
          "MALE_BREAST_EXPOSED (0.77)" in fixed, "(got %r)" % fixed)
    check("multi-object-best-score-wins-RED",
          "MALE_BREAST_EXPOSED (0.74)" in red,
          "(legacy got %r; must show 0.74)" % red)

    # ---------------------------------------------------------------- check 3
    # Regression guard: the single-object clip that already rendered correctly
    # must be unchanged, modulo score formatting.
    check("single-object-unchanged",
          render(_kClip0859) ==
          "MALE_BREAST_EXPOSED (0.75), MALE_GENITALIA_EXPOSED (0.44)",
          "(got %r)" % render(_kClip0859))

    # ---------------------------------------------------------------- check 4
    # Highest score leads, whichever class it is.
    check("ordering-highest-first",
          render(["MALE_BREAST_EXPOSED=0.20,MALE_GENITALIA_EXPOSED=0.90"])
          .startswith("MALE_GENITALIA_EXPOSED"),
          "(got %r)" % render(["MALE_BREAST_EXPOSED=0.20,"
                               "MALE_GENITALIA_EXPOSED=0.90"]))

    # ---------------------------------------------------------------- check 5
    # nudity=True with no usable detail still says something.
    for empty in (None, "", "   "):
        check("empty-detail-fallback",
              render([empty]) == "Nudity detected",
              "(%r -> %r)" % (empty, render([empty])))

    # ---------------------------------------------------------------- check 6
    # Unparseable fragments are preserved verbatim, as the old renderer did,
    # and a non-numeric score is dropped rather than raising.
    check("malformed-fragment-preserved",
          render(["SOME_TOKEN"]) == "SOME_TOKEN",
          "(got %r)" % render(["SOME_TOKEN"]))
    check("malformed-score-dropped",
          render(["A=notanumber,B=0.50"]) == "B (0.50)",
          "(got %r)" % render(["A=notanumber,B=0.50"]))
    check("malformed-fragment-deduped",
          render(["SOME_TOKEN", "SOME_TOKEN"]) == "SOME_TOKEN",
          "(got %r)" % render(["SOME_TOKEN", "SOME_TOKEN"]))

    # ---------------------------------------------------------------- check 7
    # Scores render at two decimals, so 0.3 reads 0.30 as the stored string did.
    check("score-format-two-dp",
          render(["X=0.3"]) == "X (0.30)", "(got %r)" % render(["X=0.3"]))

    # ---------------------------------------------------------------- check 8
    # Merging is order-independent -- the bug was an ordering accident.
    check("order-independent",
          render(_kClip0856) == render(list(reversed(_kClip0856))),
          "(forward %r vs reversed %r)"
          % (render(_kClip0856), render(list(reversed(_kClip0856)))))

    return failures


def main():
    ap = argparse.ArgumentParser(description=__doc__,
            formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="print passing checks too")
    args = ap.parse_args()

    try:
        from frontEnd.SearchDetectionPanel import (mergeNudityDetail,
                                                   formatNudityDetail)
    except Exception as e:
        print("SKIP: cannot import frontEnd.SearchDetectionPanel (%s)" % e)
        return 2

    print("testNudityMerge: checking nudity class merge for the search panel")
    failures = _run(mergeNudityDetail, formatNudityDetail, args.verbose)

    if failures:
        print("\n%d check(s) FAILED" % len(failures))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
