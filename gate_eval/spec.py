"""Gate specs: what the judge is asked, and how the prompt is rendered.

A gate spec is a JSON file (schema "llm-gate-eval/gate@1"):

    id             short identifier, also the name of its sincerity corpus file
    criterion_key  stable key for the idea the player must express
    title          one-line human description
    context        the scene, in a few sentences
    criterion      what the player's free text must genuinely express
    judge_prompt   {"system": ..., "user": ...} templates; placeholders are
                   {{context}}, {{criterion}} and {{text}}
    keywords       optional; used ONLY by the naive keyword baseline judge
    source         optional provenance note
"""
import hashlib
import json
import re

GATE_SCHEMA = "llm-gate-eval/gate@1"
REQUIRED_STR = ("id", "criterion_key", "title", "context", "criterion")
PLACEHOLDERS = ("{{context}}", "{{criterion}}", "{{text}}")

# Strings in player text that could break the prompt's structure: our own
# delimiter tags, and ChatML control tokens (a raw /completion prompt would
# otherwise let the player open a fake system or assistant turn).
_NEUTRALIZE = re.compile(r"</?\s*player_text\s*>|<\|im_(?:start|end)\|>|<\|endoftext\|>",
                         re.IGNORECASE)


class SpecError(ValueError):
    pass


def canonical_sha256(obj):
    """SHA-256 of a JSON value in canonical form (sorted keys, no whitespace)."""
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def validate_gate(spec):
    """Return a list of problems (empty list = valid)."""
    errs = []
    if not isinstance(spec, dict):
        return ["gate spec must be a JSON object"]
    if spec.get("schema") != GATE_SCHEMA:
        errs.append(f"schema must be {GATE_SCHEMA!r}")
    for k in REQUIRED_STR:
        if not isinstance(spec.get(k), str) or not spec[k].strip():
            errs.append(f"{k} must be a non-empty string")
    jp = spec.get("judge_prompt")
    if not isinstance(jp, dict) or not all(isinstance(jp.get(k), str) for k in ("system", "user")):
        errs.append("judge_prompt must have string 'system' and 'user' templates")
    else:
        joined = jp["system"] + jp["user"]
        for ph in PLACEHOLDERS:
            if ph not in joined:
                errs.append(f"judge_prompt is missing placeholder {ph}")
        if "{{text}}" in jp["system"]:
            errs.append("{{text}} belongs in the user template, not the system template")
    kw = spec.get("keywords", [])
    if not isinstance(kw, list) or not all(isinstance(w, str) for w in kw):
        errs.append("keywords must be a list of strings")
    return errs


def load_gate(path):
    with open(path, encoding="utf-8") as f:
        spec = json.load(f)
    errs = validate_gate(spec)
    if errs:
        raise SpecError(f"{path}: " + "; ".join(errs))
    return spec


def neutralize(text):
    return _NEUTRALIZE.sub("[removed]", text)


def render(spec, text):
    """Return (system, user) prompt strings for one piece of player text."""
    jp = spec["judge_prompt"]

    def fill(tpl):
        # Plain replace (not str.format) so braces in player text are inert,
        # and {{text}} last so player text can't smuggle in the other fields.
        out = tpl.replace("{{context}}", spec["context"]).replace("{{criterion}}", spec["criterion"])
        return out.replace("{{text}}", neutralize(text))

    return fill(jp["system"]), fill(jp["user"])
