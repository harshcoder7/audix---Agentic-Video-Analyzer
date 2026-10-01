"""Known ASR/model mishearings corrected in text that reaches the user.

'Zamp' -- an internal product name, not a dictionary word -- gets misheard by
both whisper.cpp (local transcription) and Gemini's own audio/video
understanding (used during graph extraction), landing as 'XAMPP', 'XAMP', or
'ZAMP' instead. Both models default to the nearest *common* word/casing they
actually know (XAMPP is a well-known dev-tool acronym) rather than a
proprietary name they've never seen -- a standard ASR out-of-vocabulary
failure mode, not something specific to this app.

Applied at both points a mishearing can originate: ingestion/audio.py
(whisper.cpp's own transcription) and extraction/gemini_extract.py (Gemini's
independent read of the raw video) -- each hears the audio itself, so each
needs the same correction. Never applied to node `id` fields (schema.py) --
edges and stored chat citations reference those ids by exact string, and
rewriting them would silently break that linkage.
"""

import re

TERM_CORRECTIONS = {
    r"\b(?:xampp|xamp|zamp)\b": "Zamp",
}

_COMPILED = [(re.compile(pattern, re.IGNORECASE), replacement) for pattern, replacement in TERM_CORRECTIONS.items()]


def correct_known_terms(text: str) -> str:
    if not text:
        return text
    for pattern, replacement in _COMPILED:
        text = pattern.sub(replacement, text)
    return text


# Every chat/AOP/project/workspace answer is forced into a JSON schema
# ({"answer": {"type": "string"}, ...}) rather than returned as free text --
# needed so citations/suggestions/sources come back as real structured
# fields alongside it. Observed directly in stored chat threads: when asked
# to fill a single JSON string field, the model sometimes drops the real
# newlines a markdown bullet list needs, flattening "- **Label**: text"
# items into one run-on line separated only by a space -- e.g.
# "...include:  - **Overview**: ... - **Role**: ..." with no \n anywhere.
# The prompts now explicitly ask for real newlines (the actual fix), but
# that's a probabilistic instruction, not a guarantee -- this is the
# deterministic backstop: " - **" is an extremely specific, almost never
# naturally-occurring sequence in ordinary prose (nobody writes an em-dash
# immediately followed by a bold marker), so inserting a line break there
# is safe and fixes exactly the failure mode actually observed, without
# touching normal sentences that happen to contain a plain " - " dash.
_FLATTENED_BULLET = re.compile(r"(?<!\n)\s-\s(?=\*\*)")


def repair_flattened_bullets(text: str) -> str:
    if not text:
        return text
    return _FLATTENED_BULLET.sub("\n- ", text)
