"""
ResponseSubstitution.py

Shared substitution-variable engine for rule responses.

Variables (all optional; unknown tokens are left untouched):
  {SvRuleName}     name of the rule that fired
  {SvCameraName}   camera location
  {SvEventTime}    event time, local "%Y-%m-%d %H:%M:%S"
  {SvRuleLookFor}  the rule's configured "Look for" target, e.g. "People" or
                   "Faces: Bernie, Alice"
  {SvRuleFace}     face name(s) recognized on the triggering object(s) at fire
                   time; "Unknown" if a face was detected but not recognized;
                   "" if no face

Values are substituted UNQUOTED.  (WebhookResponse keeps its historical
behavior of single-quoting {SvRuleName}/{SvCameraName} in webhook content —
that quirk lives there, not here, so existing webhooks are unaffected.)

Pure stdlib — importable from response classes and the ResponseRunner alike.
"""

import time


def substituteResponseVars(text, ruleName=None, camLoc=None, eventTimeMs=None,
                           lookFor=None, faceName=None):
    """Replace the {Sv*} substitution variables in a text template.

    @param  text         The template text (returned unchanged if falsy).
    @param  ruleName     Rule name, or None to leave {SvRuleName} untouched.
    @param  camLoc       Camera location, or None to leave it untouched.
    @param  eventTimeMs  Event epoch ms, or None to leave it untouched.
    @param  lookFor      Look-for description; None substitutes "".
    @param  faceName     Face name(s) string; None substitutes "".
    @return text         The substituted text.
    """
    if not text:
        return text

    if ruleName is not None:
        text = text.replace("{SvRuleName}", ruleName)
    if camLoc is not None:
        text = text.replace("{SvCameraName}", camLoc)
    if eventTimeMs is not None:
        timeStr = time.strftime('%Y-%m-%d %H:%M:%S',
                                time.localtime(eventTimeMs / 1000.0))
        text = text.replace("{SvEventTime}", timeStr)
    text = text.replace("{SvRuleLookFor}", lookFor or "")
    text = text.replace("{SvRuleFace}", faceName or "")

    return text


def faceNameForObjs(dataMgr, objIds):
    """Resolve the {SvRuleFace} value for a set of triggering objects.

    @param  dataMgr  A DataManager (may be None).
    @param  objIds   Iterable of object db ids.
    @return name     Unique recognized name(s) joined with ", "; "Unknown" when
                     a face was detected but none recognized; "" when no face
                     (or on any failure).
    """
    if dataMgr is None or not objIds:
        return ""
    try:
        attrDicts = dataMgr.getObjectAttributes(list(objIds))
    except Exception:
        return ""

    names = []
    sawFace = False
    for attrs in (attrDicts or {}).values():
        if not attrs:
            continue
        if attrs.get('faceDetConf') is not None:
            sawFace = True
        name = attrs.get('faceName')
        if name and name not in names:
            names.append(name)

    if names:
        return ", ".join(names)
    return "Unknown" if sawFace else ""
