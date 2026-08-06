"""
Core logic for scanning a music library and working out what each file's
name should be per "Music Library Naming Convention_updated - Naming.csv"
— including deriving Artist from the folder path when the filename alone
doesn't have one (formatting fixes and junk-stripping only — never
guessing new Title/Artist/Album info that isn't already in the filename
or folder path).

UI-agnostic: no GUI toolkit is imported here. list_songs_by_category.py
(Tkinter) and streamlit_app.py (Streamlit) both build on this module.

Read-only: nothing on disk is renamed, copied, or moved by these functions
except apply_renames(), which only ever writes into a new output folder,
never touching the originals.
"""

import csv
import difflib
import os
import re
import shutil

AUDIO_EXTENSIONS = {
    ".mp3", ".wav", ".flac", ".aac", ".m4a", ".ogg", ".wma",
    ".aiff", ".alac", ".opus", ".amr", ".mp4", ".mov",
}

# Re-scanning a folder that already had "Apply Renames" run on it (output
# folders land right next to what you scanned, so a later, higher-level
# scan can walk straight into them) would otherwise reprocess already-
# renamed files and mangle them further — e.g. treating the output
# folder's own "- Renamed Output <timestamp>" name as an Album. Both the
# output folders and their report CSVs are recognized by name and skipped.
_OUTPUT_FOLDER_PATTERN = re.compile(r" - Renamed Output \d{8}_\d{6}$")
_REPORT_FILENAME_PATTERN = re.compile(r"^Song_List_By_Category_\d{8}_\d{6}\.csv$")

# Segment that stops at a " - " boundary (Title/Artist/Album/Event/Project/Lyricist).
D = r"(?:(?!\s-\s).)+"
# Segment inside parentheses / before a trailing comma (Word, Lang, Type, Duration, Year, ...).
P = r"[^,()]+"

# Ordered most-specific first: classify() returns the first match, so a
# keyword/paren-bearing pattern must be checked before the generic
# dash-only shapes it would otherwise also satisfy.
_RAW_PATTERNS = [
    ("5. SRMD Bhakti - No Album", rf"^{D} - {D}, SRMD Bhakti$"),
    ("6. SRMD Bhakti - With Album", rf"^{D} - {D} - {D}, SRMD Bhakti$"),
    ("7. SRMD Recording (Internal)", rf"^{D} - {D}, SRMD$"),
    ("8. SRMD Project-based (Title - Project - Artist, SRMD)", rf"^{D} - {D} - {D}, SRMD$"),
    ("9. SRMD Version - Same Title", rf"^{D} \(SRMD\) - {D}$"),
    ("10. SRMD Version - Different Title", rf"^{D} \({P} - SRMD\)\s*$"),
    ("11. Original (OG)", rf"^{D} \(OG\) - {D}$"),
    ("12. SRMD Version - Same Tune as OG", rf"^{D} \({P} Tune\), SRMD$"),
    ("13. Length Variant (Short/Extended)", rf"^{D} \((Short|Extended)\)$"),
    ("14. Loop - No Duration", rf"^{D} \(Loop\)\s*,\s*SRMD$"),
    ("15. Loop - With Duration", rf"^{D} \(Loop {P}\) - {D}$"),
    ("16. Dhun - Suffix Form", rf"^{D} \(Dhun\), SRMD$"),
    ("16. Dhun - Type Form", rf"^{D} \({P} Dhun\)(?: - {D})?$"),
    ("17. Instrumental", rf"^{D} \({P} Instru\), SRMD$"),
    ("18. Minus / Karaoke", rf"^{D} \(Minus\)\s*$"),
    ("19. Live Recording", rf"^{D} \(Live\) - {D}, SRMD$"),
    ("20. Language Variant", rf"^{D} \({P}\) - {D}, SRMD$"),
    ("21. Event-specific Edit", rf"^{D} \({P} Edit\)\s*(?:- {D})?$"),
    ("22. Words Removed (wo)", rf"^{D} \(wo {P}\) - {D}$"),
    ("23. Words Added (w)", rf"^{D} \(w {P}\) - {D}$"),
    ("24. Mangalacharan", rf"^Mangalacharan - {D} - {D}, SRMD$"),
    ("25. Arti - Standard (dash form)", rf"^Arti - {D} - {D} - {D}, SRMD$"),
    ("25. Arti - Standard (Title (Arti - Event) - Artist)", rf"^{D} \(Arti - {D}\) - {D}$"),
    ("26. Arti - Year-based (dash form)", rf"^Arti {P} - {D} - {D}, SRMD$"),
    ("26. Arti - Year-based (Title (Arti Year) - Artist)", rf"^{D} \(Arti {P}\) - {D}$"),
    ("27. Do Not Play / Restricted", rf"^! {D} - {D}(?:, SRMD Bhakti)?$"),
    ("28. Mashup", rf"^{D} x {D} - {D}$"),
    ("4. Event Recording / 8. SRMD Project (Title (Event/Project) - Artist)", rf"^{D} \({P}\) - {D}$"),
    # Generic catch-alls last: they'd otherwise swallow every match above.
    ("3. Album Track / 4. Event Recording / 8. SRMD Project / 29. Lyricist (Title - X - Y)",
     rf"^{D} - {D} - {D}$"),
    ("2. Singles - With Artist (Title - Artist)", rf"^{D} - {D}$"),
    ("1. Singles - No Artist (Title only)", rf"^{D}$"),
]

_seen = set()
PATTERNS = []
for label, pattern in _RAW_PATTERNS:
    if pattern in _seen:
        continue
    _seen.add(pattern)
    PATTERNS.append((label, re.compile(pattern, re.IGNORECASE)))

UNMATCHED_LABEL = "Needs Review - No Matching Pattern"
OTHER_LABEL = "Other / Non-audio File"

# Every category from the naming-convention sheet, for manual override: pick
# one explicitly instead of relying on auto-detection. "template" builds the
# New Name with str.format() from the cleaned filename Title plus whatever
# extra fields the user fills in; "fields" lists those extras in the order
# they should be asked for (Title/Title2 aside — Title always comes from the
# cleaned filename; Title2 is manual since a filename can't be reliably
# auto-split into two song titles for the Mashup category).
CATEGORY_PATTERNS = [
    {"name": "1. Singles - No Artist", "pattern_display": "<Title>",
     "template": "{Title}", "fields": []},
    {"name": "2. Singles - With Artist", "pattern_display": "<Title> - <Artist>",
     "template": "{Title} - {Artist}", "fields": ["Artist"]},
    {"name": "3. Album Track", "pattern_display": "<Title> - <Album> - <Artist>",
     "template": "{Title} - {Album} - {Artist}", "fields": ["Album", "Artist"]},
    {"name": "4. Event Recording (dash form)", "pattern_display": "<Title> - <Event> - <Artist>",
     "template": "{Title} - {Event} - {Artist}", "fields": ["Event", "Artist"]},
    {"name": "4. Event Recording (bracket form)", "pattern_display": "<Title> (<Event>) - <Artist>",
     "template": "{Title} ({Event}) - {Artist}", "fields": ["Event", "Artist"]},
    {"name": "5. SRMD Bhakti - No Album", "pattern_display": "<Title> - <Artist>, SRMD Bhakti",
     "template": "{Title} - {Artist}, SRMD Bhakti", "fields": ["Artist"]},
    {"name": "6. SRMD Bhakti - With Album", "pattern_display": "<Title> - <Album> - <Artist>, SRMD Bhakti",
     "template": "{Title} - {Album} - {Artist}, SRMD Bhakti", "fields": ["Album", "Artist"]},
    {"name": "7. SRMD Recording (Internal)", "pattern_display": "<Title> - <Artist>, SRMD",
     "template": "{Title} - {Artist}, SRMD", "fields": ["Artist"]},
    {"name": "8. SRMD Project-based (dash form)", "pattern_display": "<Title> - <Project> - <Artist>, SRMD",
     "template": "{Title} - {Project} - {Artist}, SRMD", "fields": ["Project", "Artist"]},
    {"name": "8. SRMD Project-based (bracket form)", "pattern_display": "<Title> (<Project>) - <Artist>",
     "template": "{Title} ({Project}) - {Artist}", "fields": ["Project", "Artist"]},
    {"name": "9. SRMD Version - Same Title", "pattern_display": "<Title> (SRMD) - <Artist>",
     "template": "{Title} (SRMD) - {Artist}", "fields": ["Artist"]},
    {"name": "10. SRMD Version - Different Title", "pattern_display": "<Title> (<Original> - SRMD)",
     "template": "{Title} ({Original} - SRMD)", "fields": ["Original"]},
    {"name": "11. Original (OG)", "pattern_display": "<Title> (OG) - <Album>",
     "template": "{Title} (OG) - {Album}", "fields": ["Album"]},
    {"name": "12. SRMD Version - Same Tune as OG", "pattern_display": "<Title> (<OG Title> Tune), SRMD",
     "template": "{Title} ({OGTitle} Tune), SRMD", "fields": ["OGTitle"]},
    {"name": "13. Length Variant (Short)", "pattern_display": "<Title> (Short)",
     "template": "{Title} (Short)", "fields": []},
    {"name": "13. Length Variant (Extended)", "pattern_display": "<Title> (Extended)",
     "template": "{Title} (Extended)", "fields": []},
    {"name": "14. Loop - No Duration", "pattern_display": "<Title> (Loop), SRMD",
     "template": "{Title} (Loop), SRMD", "fields": []},
    {"name": "15. Loop - With Duration", "pattern_display": "<Title> (Loop <Duration>) - <Artist>, SRMD",
     "template": "{Title} (Loop {Duration}) - {Artist}, SRMD", "fields": ["Duration", "Artist"]},
    {"name": "16. Dhun (suffix form)", "pattern_display": "<Title> (Dhun), SRMD",
     "template": "{Title} (Dhun), SRMD", "fields": []},
    {"name": "16. Dhun (type form)", "pattern_display": "<Title> (<Type> Dhun) - <Artist>",
     "template": "{Title} ({Type} Dhun) - {Artist}", "fields": ["Type", "Artist"]},
    {"name": "17. Instrumental", "pattern_display": "<Title> (<Type> Instru), SRMD",
     "template": "{Title} ({Type} Instru), SRMD", "fields": ["Type"]},
    {"name": "18. Minus / Karaoke", "pattern_display": "<Title> (Minus)",
     "template": "{Title} (Minus)", "fields": []},
    {"name": "19. Live Recording", "pattern_display": "<Title> (Live) - <Artist>, SRMD",
     "template": "{Title} (Live) - {Artist}, SRMD", "fields": ["Artist"]},
    {"name": "20. Language Variant", "pattern_display": "<Title> (<Lang>) - <Artist>, SRMD",
     "template": "{Title} ({Lang}) - {Artist}, SRMD", "fields": ["Lang", "Artist"]},
    {"name": "21. Event-specific Edit", "pattern_display": "<Title> (<Event> Edit)",
     "template": "{Title} ({Event} Edit)", "fields": ["Event"]},
    {"name": "22. Words Removed (wo)", "pattern_display": "<Title> (wo <Word>) - <Artist>",
     "template": "{Title} (wo {Word}) - {Artist}", "fields": ["Word", "Artist"]},
    {"name": "23. Words Added (w)", "pattern_display": "<Title> (w <Word>) - <Artist>",
     "template": "{Title} (w {Word}) - {Artist}", "fields": ["Word", "Artist"]},
    {"name": "24. Mangalacharan", "pattern_display": "Mangalacharan - <Event> - <Artist>, SRMD",
     "template": "Mangalacharan - {Event} - {Artist}, SRMD", "fields": ["Event", "Artist"]},
    {"name": "25. Arti - Standard (dash form)", "pattern_display": "Arti - <Event> - <Title> - <Artist>, SRMD",
     "template": "Arti - {Event} - {Title} - {Artist}, SRMD", "fields": ["Event", "Artist"]},
    {"name": "25. Arti - Standard (bracket form)", "pattern_display": "<Title> (Arti - <Event>) - <Artist>",
     "template": "{Title} (Arti - {Event}) - {Artist}", "fields": ["Event", "Artist"]},
    {"name": "26. Arti - Year-based (dash form)", "pattern_display": "Arti <Year> - <Title> - <Artist>, SRMD",
     "template": "Arti {Year} - {Title} - {Artist}, SRMD", "fields": ["Year", "Artist"]},
    {"name": "26. Arti - Year-based (bracket form)", "pattern_display": "<Title> (Arti <Year>) - <Artist>",
     "template": "{Title} (Arti {Year}) - {Artist}", "fields": ["Year", "Artist"]},
    {"name": "27. Do Not Play / Restricted", "pattern_display": "! <Title> - <Artist>",
     "template": "! {Title} - {Artist}", "fields": ["Artist"]},
    {"name": "28. Mashup", "pattern_display": "<Title 1> x <Title 2> - <Event>",
     "template": "{Title} x {Title2} - {Event}", "fields": ["Title2", "Event"]},
    {"name": "29. Lyricist", "pattern_display": "<Title> - <Artist> - <Lyricist>",
     "template": "{Title} - {Artist} - {Lyricist}", "fields": ["Artist", "Lyricist"]},
]

FIELD_LABELS = {
    "Artist": "Artist",
    "Album": "Album",
    "Event": "Event",
    "Project": "Project",
    "Duration": "Duration (e.g. 1hr, 30min)",
    "Lang": "Language (Guj/Hin/Eng/San/Mar)",
    "Word": "Word",
    "Lyricist": "Lyricist",
    "Year": "Year",
    "Original": "Original Title",
    "OGTitle": "OG Title",
    "Type": "Type (e.g. Slow, Flute)",
    "Title2": "Second Title (for Mashup)",
}

AUTO_DETECT_LABEL = "Auto-detect (recommended)"

_CATEGORY_BY_NAME = {cat["name"]: cat for cat in CATEGORY_PATTERNS}

# Maps an auto-classify() label to the manual CATEGORY_PATTERNS name(s) it
# corresponds to, for the "detect from example filename" feature below.
# Most labels map 1:1; a handful of shapes are shared by more than one
# category (e.g. "<Title> (<X>) - <Artist>" fits both Event Recording and
# SRMD Project bracket forms) and list every candidate so the caller can
# ask the user to pick between them.
_CATEGORY_NAMES_BY_LABEL = {
    "5. SRMD Bhakti - No Album": ["5. SRMD Bhakti - No Album"],
    "6. SRMD Bhakti - With Album": ["6. SRMD Bhakti - With Album"],
    "7. SRMD Recording (Internal)": ["7. SRMD Recording (Internal)"],
    "8. SRMD Project-based (Title - Project - Artist, SRMD)": ["8. SRMD Project-based (dash form)"],
    "9. SRMD Version - Same Title": ["9. SRMD Version - Same Title"],
    "10. SRMD Version - Different Title": ["10. SRMD Version - Different Title"],
    "11. Original (OG)": ["11. Original (OG)"],
    "12. SRMD Version - Same Tune as OG": ["12. SRMD Version - Same Tune as OG"],
    "13. Length Variant (Short/Extended)": ["13. Length Variant (Short)", "13. Length Variant (Extended)"],
    "14. Loop - No Duration": ["14. Loop - No Duration"],
    "15. Loop - With Duration": ["15. Loop - With Duration"],
    "16. Dhun - Suffix Form": ["16. Dhun (suffix form)"],
    "16. Dhun - Type Form": ["16. Dhun (type form)"],
    "17. Instrumental": ["17. Instrumental"],
    "18. Minus / Karaoke": ["18. Minus / Karaoke"],
    "19. Live Recording": ["19. Live Recording"],
    "20. Language Variant": ["20. Language Variant"],
    "21. Event-specific Edit": ["21. Event-specific Edit"],
    "22. Words Removed (wo)": ["22. Words Removed (wo)"],
    "23. Words Added (w)": ["23. Words Added (w)"],
    "24. Mangalacharan": ["24. Mangalacharan"],
    "25. Arti - Standard (dash form)": ["25. Arti - Standard (dash form)"],
    "25. Arti - Standard (Title (Arti - Event) - Artist)": ["25. Arti - Standard (bracket form)"],
    "26. Arti - Year-based (dash form)": ["26. Arti - Year-based (dash form)"],
    "26. Arti - Year-based (Title (Arti Year) - Artist)": ["26. Arti - Year-based (bracket form)"],
    "27. Do Not Play / Restricted": ["27. Do Not Play / Restricted"],
    "28. Mashup": ["28. Mashup"],
    "4. Event Recording / 8. SRMD Project (Title (Event/Project) - Artist)": [
        "4. Event Recording (bracket form)", "8. SRMD Project-based (bracket form)",
    ],
    "3. Album Track / 4. Event Recording / 8. SRMD Project / 29. Lyricist (Title - X - Y)": [
        "3. Album Track", "4. Event Recording (dash form)", "8. SRMD Project-based (dash form)", "29. Lyricist",
    ],
    "2. Singles - With Artist (Title - Artist)": ["2. Singles - With Artist"],
    "1. Singles - No Artist (Title only)": ["1. Singles - No Artist"],
}


def detect_category_candidates(example_name):
    """
    Given one example filename (with or without extension) typed by the
    user, classify it the same way auto-detect would and return the
    matching manual-category entries from CATEGORY_PATTERNS.

    Returns (candidates, error): error is a user-facing message when
    nothing matched at all, in which case candidates is []. candidates has
    more than one entry only for shapes genuinely shared by multiple
    categories (see _CATEGORY_NAMES_BY_LABEL) - the caller should ask the
    user to pick one in that case.
    """
    name_no_ext, _ = os.path.splitext(example_name.strip())
    if not name_no_ext:
        name_no_ext = example_name.strip()
    normalized = _normalize_casing(_normalize_spacing(name_no_ext))

    label = classify(normalized)
    if not label:
        return [], "No matching pattern found for that example filename."

    names = _CATEGORY_NAMES_BY_LABEL.get(label, [])
    if not names:
        return [], f"Matched \"{label}\", which has no manual-category equivalent."

    if len(names) > 1 and label.startswith("13. Length Variant"):
        preferred = "extended" if "extended" in normalized.lower() else "short"
        names = [n for n in names if preferred in n.lower()] or names

    candidates = [_CATEGORY_BY_NAME[n] for n in names if n in _CATEGORY_BY_NAME]
    return candidates, None


_DIGIT_RUN = re.compile(r"\d+(?:\.\d+)?")


def _generalize_span_to_regex(text):
    """Turn a literal snippet removed from the example into a regex
    fragment where digit runs become a wildcard - so a per-file track
    number or duration (e.g. "01" vs "05", "24min" vs "26.53min") still
    matches - and everything else (dashes, spaces, "min", ...) is matched
    literally."""
    parts = []
    pos = 0
    for m in _DIGIT_RUN.finditer(text):
        parts.append(re.escape(text[pos:m.start()]))
        parts.append(r"\d+(?:\.\d+)?")
        pos = m.end()
    parts.append(re.escape(text[pos:]))
    return "".join(parts)


def _capitalize_first_letters(text):
    """Uppercase each word's first letter, leaving the rest of the word
    untouched - so an existing acronym like "SRMD" survives, unlike
    str.title() which would flatten it to "Srmd"."""
    return re.sub(r"(?<![A-Za-z])([a-z])", lambda m: m.group(1).upper(), text)


def _infer_group_transform(before_span, after_span):
    """
    A "kept" span lines up between before/after only because it matches
    case-insensitively (build_custom_rule diffs on lowercased text) - the
    original-case text can still differ, e.g. "jan pavani" -> "Jan Pavani".
    Figure out which casing rule explains that difference, if any, so it
    can be reapplied to each file's OWN text instead of forcing this one
    example's literal words onto every other file. Returns a callable, or
    None if the span's case didn't change (or changed in some way that
    doesn't generalize, in which case the file's own text is kept as-is).
    """
    if before_span == after_span:
        return None
    if after_span == before_span.upper():
        return str.upper
    if after_span == before_span.lower():
        return str.lower
    if after_span == _capitalize_first_letters(before_span):
        return _capitalize_first_letters
    return None


def build_custom_rule(before_example, after_example):
    """
    Learn a filename transformation from a single (before, after) example
    pair, generalized so it applies to every other file in the folder
    instead of just the one example given:
      - text common to both before and after (e.g. the song title) is kept
        as a wildcard placeholder - each file's own version of it is
        carried through, not forced to match the example's literal text.
        If the example's casing differs between before/after (e.g. title-
        casing each word), that same casing rule is reapplied to each
        file's own text rather than the example's words.
      - text removed from before (e.g. a leading track number, a trailing
        duration code) is dropped, with any digit runs inside it
        generalized so a different number in another file still matches.
      - text added in after that wasn't in before is inserted literally
        and identically for every file.

    Returns a function apply_fn(name_no_ext) -> new name, or None if that
    filename doesn't fit the learned shape at all.

    Raises ValueError if the example has nothing in common between before
    and after to anchor the rule on (nothing to generalize from).
    """
    before_noext, _ = os.path.splitext(before_example.strip())
    after_noext, _ = os.path.splitext(after_example.strip())
    if not before_noext:
        before_noext = before_example.strip()
    if not after_noext:
        after_noext = after_example.strip()

    # Diffed case-insensitively so a pure casing change (e.g. "jan" vs
    # "Jan") still lines up as one "kept" span instead of splitting into
    # unrelated deletes/inserts around the differing letters.
    opcodes = difflib.SequenceMatcher(
        None, before_noext.lower(), after_noext.lower(), autojunk=False
    ).get_opcodes()

    pattern_parts = []
    segments = []  # ordered ("group", n, transform) / ("literal", text, None) specs
    group_count = 0

    for tag, i1, i2, j1, j2 in opcodes:
        before_span = before_noext[i1:i2]
        after_span = after_noext[j1:j2]
        if tag == "equal":
            group_count += 1
            pattern_parts.append("(.+?)")
            segments.append(("group", group_count, _infer_group_transform(before_span, after_span)))
        elif tag == "delete":
            pattern_parts.append(_generalize_span_to_regex(before_span))
        elif tag == "insert":
            if after_span:
                segments.append(("literal", after_span, None))
        elif tag == "replace":
            pattern_parts.append(_generalize_span_to_regex(before_span))
            if after_span:
                segments.append(("literal", after_span, None))

    if group_count == 0:
        raise ValueError(
            "That example's before/after names have nothing in common — "
            "there's nothing to carry over for other files."
        )

    pattern = re.compile("^" + "".join(pattern_parts) + "$", re.IGNORECASE)

    def apply_fn(name_no_ext):
        m = pattern.match(name_no_ext.strip())
        if not m:
            return None
        parts = []
        for kind, value, transform in segments:
            if kind == "group":
                text = m.group(value)
                parts.append(transform(text) if transform else text)
            else:
                parts.append(value)
        return "".join(parts)

    return apply_fn


# The sheet's "Title only" category has no real structure (a bare title is
# valid by definition), so without this it would report obvious junk
# filenames as legitimate tracks. Checked against an underscore-as-space
# copy of the name so "_final_" / "_v2_" still match \b word boundaries.
_JUNK_HINTS = re.compile(
    r"whatsapp|voice\s*(memo|note)|recording|img[-_ ]?\d|vid[-_ ]?\d|dsc[-_ ]?\d|"
    r"\btrack\s*\d*\b|untitled|\bcopy\b|\bfinal\b|\bv\d+\b|\bexport\b|"
    r"\d{4}\s*[-/]\s*\d{2}\s*[-/]\s*\d{2}|\d{2}\s*[-/]\s*\d{2}\s*[-/]\s*\d{4}|"  # date-like
    r"\d{1,2}\s*[.:]\s*\d{2}\s*[.:]\s*\d{2}",  # time-like
    re.IGNORECASE,
)


def classify(name_without_ext):
    """Return the matching pattern label, or None if nothing matches."""
    name = name_without_ext.strip()
    junk_check_name = name.replace("_", " ")
    for label, regex in PATTERNS:
        if regex.match(name):
            if label.startswith("1. Singles - No Artist") and _JUNK_HINTS.search(junk_check_name):
                return None
            return label
    return None


# Tokens that show up with inconsistent casing/spacing but have one correct
# form in the convention (e.g. "srmd" -> "SRMD"). Longest keys are tried
# first so "srmd bhakti" matches before the bare "srmd".
_CANONICAL_WORDS = {
    "srmd bhakti": "SRMD Bhakti",
    "srmd": "SRMD",
    "og": "OG",
    "loop": "Loop",
    "dhun": "Dhun",
    "live": "Live",
    "minus": "Minus",
    "arti": "Arti",
    "instru": "Instru",
    "extended": "Extended",
    "short": "Short",
    "mangalacharan": "Mangalacharan",
}


# A number followed by punctuation ("01.", "04-", "04)") doesn't need a
# trailing space to count as a track-number prefix — "01.Introduction" is
# just as much a glued track code as "01. Introduction" is. A bare number
# or a letter+digit side-code with NO punctuation ("04 Title", "A1 Title")
# still requires a trailing space, so we don't eat real content that
# happens to start with a number (best-effort only — see clean_title docs).
_LEADING_CODE = re.compile(
    r"^\s*(?:\d{1,4}[.\-)]\s*|\d{1,4}\s+|[A-Za-z]{1,2}\d{1,3}\s+)"
)


def _strip_leading_codes(name):
    """Drop leading track-number / side-or-take codes glued before the
    title, e.g. "278 A1 Title" -> "Title". These aren't Title/Artist/Album
    content per the naming sheet, so they're dropped, not just reformatted.
    Loops since more than one code can be stacked (a track number followed
    by a side code, as in the example above)."""
    while True:
        new_name = _LEADING_CODE.sub("", name)
        if new_name == name:
            return name
        name = new_name


_TRAILING_DATE_CODE = re.compile(r"\s*-\s*\d{1,2}\.\d{2}(?:\.\d{2,4})?\s*$")
# "- 24min" / "- 26.53min" glued onto the end of a name (a recording
# duration, not Title/Artist/Album/Event content per the naming sheet).
_TRAILING_DURATION_CODE = re.compile(r"\s*-\s*\d+(?:\.\d+)?\s*mins?\s*$", re.IGNORECASE)


def _strip_trailing_codes(name):
    """Drop a trailing dash+date-like or dash+duration code glued to the
    title end, e.g. "Title-27.01", "Title - 27.01.24", or
    "Title - 24min" -> "Title". Run before dash normalization, while the
    code is still recognizable as glued-on."""
    name = _TRAILING_DURATION_CODE.sub("", name)
    name = _TRAILING_DATE_CODE.sub("", name)
    return name


def _normalize_spacing(name):
    """Fix common formatting slips: leading track/side codes, trailing
    glued-on date codes, underscores, any dash used as a separator (double
    dashes, or no spaces at all, e.g. "Title-Artist"), and repeated
    internal spaces all get cleaned up — none of that is Title/Artist/
    Album/Event content per the naming sheet, so it's removed, not kept."""
    name = _strip_leading_codes(name)
    name = _strip_trailing_codes(name)
    name = name.replace("_", " ")
    name = re.sub(r"\s*-+\s*", " - ", name)
    name = re.sub(r"\s*,\s*", ", ", name)
    name = re.sub(r"(?<=\S)\(", " (", name)  # "Em(SRMD)" -> "Em (SRMD)"
    name = re.sub(r"\s{2,}", " ", name)
    return name.strip(" -,")


def _title_case_if_shouty(name):
    """If name has no lowercase letters at all (fully "SHOUTED", as old
    cassette/tape rip filenames often are), convert it to Title Case.
    Left alone otherwise, so a title that's already normal-case (maybe
    with an intentional acronym inside) isn't disturbed."""
    if re.search(r"[a-z]", name) or not re.search(r"[A-Z]", name):
        return name

    def cap_word(word):
        return word[0].upper() + word[1:].lower() if word else word

    return " ".join(cap_word(w) for w in name.split(" "))


def _normalize_casing(name):
    # Title-case a fully shouted name FIRST, then fix canonical tokens
    # (SRMD, OG, ...) back to their proper casing — otherwise "SRMD" would
    # get title-cased down to "Srmd" along with the rest of the name.
    name = _title_case_if_shouty(name)
    for key in sorted(_CANONICAL_WORDS, key=len, reverse=True):
        name = re.sub(
            rf"(?<![A-Za-z]){re.escape(key)}(?![A-Za-z])",
            _CANONICAL_WORDS[key],
            name,
            flags=re.IGNORECASE,
        )
    return name


def _strip_junk(name):
    """Remove recognized auto-generated-filename noise, leaving a bare title (if any)."""
    cleaned = _JUNK_HINTS.sub(" ", name)
    cleaned = re.sub(r"[\d.:/_-]+", " ", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" -,()")
    return cleaned


# Subfolder-name shapes confirmed against the real library as reliable
# Artist attributions. Deliberately narrow: things like "In Tune With
# Divine" or "Meditation-15 Eng" also contain "with"/a hyphen but aren't
# artist names, so only these specific shapes are trusted.
_FOLDER_ARTIST_PATTERNS = [
    re.compile(r"^Bhakti\s+with\s+(.+)$", re.IGNORECASE),
    re.compile(r"^Bhakti-(.+)$", re.IGNORECASE),
    re.compile(r"^.+?\bby\b\s+(.+)$", re.IGNORECASE),
]


def extract_artist_from_folder(folder_name):
    """Return an Artist name if folder_name matches a trusted attribution
    shape, else None. Never guesses on ambiguous/generic folder names."""
    name = folder_name.strip()
    for pattern in _FOLDER_ARTIST_PATTERNS:
        m = pattern.match(name)
        if m:
            artist = m.group(1).strip()
            # Drop a trailing date-only parenthetical, e.g. "(26.3.10)" —
            # but keep something like "(Koba)" which is part of the name.
            artist = re.sub(r"\s*\(\s*\d[\d./\-]*\s*\)\s*$", "", artist).strip()
            if artist:
                return artist
    return None


def find_folder_context(dirpath, root_folder):
    """
    Walk upward from dirpath and return (artist, album_or_event):
      - artist: from the closest ancestor folder matching a trusted Artist
        pattern (e.g. "Bhakti With X"). Includes root_folder itself — if
        you point the tool directly at an artist folder (e.g. select
        "Bhakti With Jaisinghbhai" as the folder to scan), that folder's
        own name still needs to be checked, not just its subfolders.
      - album_or_event: the file's immediate containing folder's name,
        but ONLY when the artist was found at a HIGHER ancestor, not at
        dirpath itself — i.e. there's a folder level between the file and
        its artist folder ("Bhakti-Pappaji/Rising Sun/file.mp3" — "Rising
        Sun" sits between the file and the Pappaji folder, so it's most
        likely a specific album/session under that artist, not another
        generic grouping label). None when the file sits directly in the
        artist folder, since there's nothing between them to use.
    """
    root_folder = os.path.normpath(root_folder)
    current = os.path.normpath(dirpath)
    immediate_folder_name = os.path.basename(current)

    level = 0
    while current.startswith(root_folder):
        artist = extract_artist_from_folder(os.path.basename(current))
        if artist:
            album_or_event = immediate_folder_name if level > 0 else None
            return artist, album_or_event
        if current == root_folder:
            break
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
        level += 1
    return None, None


def resolve_filename(name_no_ext, folder_artist=None, folder_album_or_event=None):
    """
    Decide the output name for a file.

    Returns (final_name, status, matched_label, used_folder_artist, used_folder_album):
      - status "OK"      -> name already matches a known pattern, unchanged
      - status "NEEDS RENAME" -> formatting fixes / junk stripping / a folder-
                             derived Artist (and Album/Event) was added to
                             salvage a valid name
      - status "REVIEW"  -> no usable title text could be found; left unchanged
    """
    original = name_no_ext.strip()
    normalized = _normalize_casing(_normalize_spacing(original))

    def _with_folder_context(title_text):
        if folder_album_or_event:
            candidate = f"{title_text} - {folder_album_or_event} - {folder_artist}"
            label = classify(candidate) or (
                "3. Album Track / 4. Event Recording / 8. SRMD Project / 29. Lyricist (Title - X - Y)"
            )
            return candidate, "NEEDS RENAME", label, True, True
        candidate = f"{title_text} - {folder_artist}"
        label = classify(candidate) or "2. Singles - With Artist (Title - Artist)"
        return candidate, "NEEDS RENAME", label, True, False

    # A junk indicator (WhatsApp export, camera dump, date/time stamp, ...)
    # anywhere in the name means it isn't safe to trust *any* structural
    # match on it — a pile of noise can accidentally line up with a
    # pattern's shape by chance. Route it to salvage instead.
    if not _JUNK_HINTS.search(normalized):
        label = classify(normalized)
        if label:
            # A bare title with no Artist/Album/Event of its own is the
            # only case where a folder-derived Artist should be added —
            # anything more specific already came from the filename itself
            # and shouldn't be overridden by a folder guess.
            if label.startswith("1. Singles - No Artist") and folder_artist:
                return _with_folder_context(normalized)
            status = "OK" if normalized == original else "NEEDS RENAME"
            return normalized, status, label, False, False

    salvaged = _strip_junk(normalized)
    if len(re.sub(r"[^A-Za-z]", "", salvaged)) >= 3:
        if folder_artist:
            return _with_folder_context(salvaged)
        label = classify(salvaged)
        return (
            salvaged, "NEEDS RENAME",
            label or "1. Singles - No Artist (Title only, auto-cleaned)",
            False, False,
        )

    return original, "REVIEW", None, False, False


def clean_title(name_no_ext):
    """
    Clean a bare Title out of a filename for the manual-category workflow:
    strip track/side codes and glued-on date codes, normalize spacing/
    casing, and salvage a title out of junk if needed.

    Returns (title, ok) — ok=False means no usable title text was found at
    all (e.g. the whole name was camera/phone-dump noise).
    """
    original = name_no_ext.strip()
    normalized = _normalize_casing(_normalize_spacing(original))

    if not _JUNK_HINTS.search(normalized):
        return normalized, True

    salvaged = _strip_junk(normalized)
    if len(re.sub(r"[^A-Za-z]", "", salvaged)) >= 3:
        return salvaged, True

    return original, False


CUSTOM_RULE_LABEL = "Custom Rule (from example)"


def process_library_custom_rule(root_folder, log, apply_fn, include_subfolders):
    """
    Apply ONE custom rename rule - learned from a single before/after
    example via build_custom_rule - to every audio file found, instead of
    matching against the naming-convention sheet's categories at all.
    Files that don't fit the learned shape are left unchanged and flagged
    for REVIEW rather than guessed at.

    Read-only: nothing on disk is renamed, copied, or moved.
    """
    root_name = os.path.basename(root_folder.rstrip(os.sep))

    if include_subfolders:
        walk_iter = os.walk(root_folder)
    else:
        walk_iter = [(root_folder, [], sorted(os.listdir(root_folder)))]

    rows = []
    for dirpath, dirnames, filenames in walk_iter:
        if include_subfolders:
            dirnames[:] = [
                d for d in dirnames
                if not d.startswith(".") and not _OUTPUT_FOLDER_PATTERN.search(d)
            ]
        rel_to_root = os.path.relpath(dirpath, root_folder)
        rel_to_root = "" if rel_to_root == "." else rel_to_root
        rel_folder = root_name if rel_to_root == "" else rel_to_root

        for filename in sorted(filenames):
            if filename.startswith(".") or not os.path.isfile(os.path.join(dirpath, filename)):
                continue
            if _REPORT_FILENAME_PATTERN.match(filename):
                continue

            name_no_ext, ext = os.path.splitext(filename)

            if ext.lower() not in AUDIO_EXTENSIONS:
                rows.append({
                    "Category": OTHER_LABEL, "Subfolder": rel_folder, "Status": "OTHER",
                    "Original Filename": filename, "New Name": filename,
                    "Artist From Folder": "", "Album/Event From Folder": "",
                    "Notes": "", "_rel_to_root": rel_to_root,
                })
                continue

            result = apply_fn(name_no_ext)
            if result is None:
                rows.append({
                    "Category": CUSTOM_RULE_LABEL, "Subfolder": rel_folder, "Status": "REVIEW",
                    "Original Filename": filename, "New Name": filename,
                    "Artist From Folder": "", "Album/Event From Folder": "",
                    "Notes": "Doesn't match the example's shape", "_rel_to_root": rel_to_root,
                })
                continue

            new_name = result.strip(" -,") + ext
            status = "OK" if new_name == filename else "NEEDS RENAME"
            rows.append({
                "Category": CUSTOM_RULE_LABEL, "Subfolder": rel_folder, "Status": status,
                "Original Filename": filename, "New Name": new_name,
                "Artist From Folder": "", "Album/Event From Folder": "",
                "Notes": "", "_rel_to_root": rel_to_root,
            })

    rows.sort(key=lambda r: (r["Category"], r["Subfolder"].lower(), r["Original Filename"].lower()))

    current_category = None
    for row in rows:
        if row["Category"] != current_category:
            current_category = row["Category"]
            count = sum(1 for r in rows if r["Category"] == current_category)
            log(f"\n=== {current_category} ({count}) ===", "header")

        location = f"{row['Subfolder']}/{row['Original Filename']}" if row["Subfolder"] else row["Original Filename"]
        if row["Status"] == "OK":
            log(f"  [OK]      {location}", "ok")
        elif row["Status"] == "NEEDS RENAME":
            log(f"  [NEEDS RENAME] {location}  ->  {row['New Name']}", "renamed")
        elif row["Status"] == "REVIEW":
            log(f"  [REVIEW]  {location}  ({row['Notes']})", "review")
        elif row["Status"] == "OTHER":
            log(f"  [OTHER]   {location}", "item")

    return rows


def process_library_manual(root_folder, log, category, field_values, include_subfolders):
    """
    Apply ONE explicitly chosen category+pattern (from CATEGORY_PATTERNS) to
    every audio file found, instead of auto-detecting per file. field_values
    is a dict of the extra fields that category's template needs (e.g.
    Artist, Album, Event) — supplied once and reused for every file. A blank
    Artist field falls back to folder-based detection per file; any other
    blank required field means that file can't be completed and is flagged
    REVIEW rather than written with a gap in it.

    Read-only: nothing on disk is renamed, copied, or moved.
    """
    root_name = os.path.basename(root_folder.rstrip(os.sep))
    template = category["template"]
    required_fields = category["fields"]

    if include_subfolders:
        walk_iter = os.walk(root_folder)
    else:
        walk_iter = [(root_folder, [], sorted(os.listdir(root_folder)))]

    rows = []
    for dirpath, dirnames, filenames in walk_iter:
        if include_subfolders:
            dirnames[:] = [
                d for d in dirnames
                if not d.startswith(".") and not _OUTPUT_FOLDER_PATTERN.search(d)
            ]
        rel_to_root = os.path.relpath(dirpath, root_folder)
        rel_to_root = "" if rel_to_root == "." else rel_to_root
        # Display path: the selected folder's own name shown instead of
        # blank for files directly in it (see process_library for why).
        # rel_to_root itself stays the true root-relative path, used later
        # to mirror this exact structure when applying renames to disk.
        rel_folder = root_name if rel_to_root == "" else rel_to_root

        folder_artist, folder_album_or_event = find_folder_context(dirpath, root_folder)

        for filename in sorted(filenames):
            if filename.startswith(".") or not os.path.isfile(os.path.join(dirpath, filename)):
                continue
            if _REPORT_FILENAME_PATTERN.match(filename):
                continue

            name_no_ext, ext = os.path.splitext(filename)

            if ext.lower() not in AUDIO_EXTENSIONS:
                rows.append({
                    "Category": OTHER_LABEL, "Subfolder": rel_folder, "Status": "OTHER",
                    "Original Filename": filename, "New Name": filename,
                    "Artist From Folder": "", "Album/Event From Folder": "",
                    "Notes": "", "_rel_to_root": rel_to_root,
                })
                continue

            title, ok = clean_title(name_no_ext)
            if not ok:
                rows.append({
                    "Category": category["name"], "Subfolder": rel_folder, "Status": "REVIEW",
                    "Original Filename": filename, "New Name": filename,
                    "Artist From Folder": "", "Album/Event From Folder": "",
                    "Notes": "No usable title text found", "_rel_to_root": rel_to_root,
                })
                continue

            values, missing, used_folder_artist, used_folder_album = {}, [], False, False
            for field in required_fields:
                val = field_values.get(field, "").strip()
                if not val and field == "Artist" and folder_artist:
                    val, used_folder_artist = folder_artist, True
                if not val and field in ("Album", "Event") and folder_album_or_event:
                    val, used_folder_album = folder_album_or_event, True
                if not val:
                    missing.append(field)
                values[field] = val

            if missing:
                missing_labels = ", ".join(FIELD_LABELS.get(f, f) for f in missing)
                rows.append({
                    "Category": category["name"], "Subfolder": rel_folder, "Status": "REVIEW",
                    "Original Filename": filename, "New Name": filename,
                    "Artist From Folder": "", "Album/Event From Folder": "",
                    "Notes": f"Missing: {missing_labels}", "_rel_to_root": rel_to_root,
                })
                continue

            new_name = template.format(Title=title, **values) + ext

            status = "OK" if new_name == filename else "NEEDS RENAME"
            rows.append({
                "Category": category["name"], "Subfolder": rel_folder, "Status": status,
                "Original Filename": filename, "New Name": new_name,
                "Artist From Folder": folder_artist if used_folder_artist else "",
                "Album/Event From Folder": folder_album_or_event if used_folder_album else "",
                "Notes": "", "_rel_to_root": rel_to_root,
            })

    rows.sort(key=lambda r: (r["Category"], r["Subfolder"].lower(), r["Original Filename"].lower()))

    current_category = None
    for row in rows:
        if row["Category"] != current_category:
            current_category = row["Category"]
            count = sum(1 for r in rows if r["Category"] == current_category)
            log(f"\n=== {current_category} ({count}) ===", "header")

        location = f"{row['Subfolder']}/{row['Original Filename']}" if row["Subfolder"] else row["Original Filename"]
        context_note = _folder_context_note(row)
        if row["Status"] == "OK":
            log(f"  [OK]      {location}", "ok")
        elif row["Status"] == "NEEDS RENAME":
            log(f"  [NEEDS RENAME] {location}  ->  {row['New Name']}{context_note}", "renamed")
        elif row["Status"] == "REVIEW":
            log(f"  [REVIEW]  {location}  ({row['Notes']})", "review")
        elif row["Status"] == "OTHER":
            log(f"  [OTHER]   {location}", "item")

    return rows


def process_library(root_folder, log):
    """
    Walk root_folder (and every subfolder), classify each audio file, and
    work out what its name should be per the naming-convention sheet.

    Read-only: nothing is renamed, copied, or moved on disk. This only
    computes what the CSV report lists in its "New Name" column.
    """
    root_name = os.path.basename(root_folder.rstrip(os.sep))

    rows = []
    for dirpath, dirnames, filenames in os.walk(root_folder):
        dirnames[:] = [
            d for d in dirnames
            if not d.startswith(".") and not _OUTPUT_FOLDER_PATTERN.search(d)
        ]
        rel_to_root = os.path.relpath(dirpath, root_folder)
        rel_to_root = "" if rel_to_root == "." else rel_to_root
        # Display path: the selected folder's own name shown instead of
        # blank for files directly in it. rel_to_root itself stays the
        # true root-relative path, used later to mirror this exact
        # structure when applying renames to disk.
        rel_folder = root_name if rel_to_root == "" else rel_to_root

        folder_artist, folder_album_or_event = find_folder_context(dirpath, root_folder)

        for filename in sorted(filenames):
            if filename.startswith("."):
                continue
            if _REPORT_FILENAME_PATTERN.match(filename):
                continue

            name_no_ext, ext = os.path.splitext(filename)

            if ext.lower() not in AUDIO_EXTENSIONS:
                category, status, new_name, used_artist, used_album = OTHER_LABEL, "OTHER", filename, False, False
            else:
                final_name, status, label, used_artist, used_album = resolve_filename(
                    name_no_ext, folder_artist, folder_album_or_event
                )
                category = label or UNMATCHED_LABEL
                new_name = filename if status == "REVIEW" else final_name + ext

            rows.append({
                "Category": category,
                "Subfolder": rel_folder,
                "Status": status,
                "Original Filename": filename,
                "New Name": new_name,
                "Artist From Folder": folder_artist if used_artist else "",
                "Album/Event From Folder": folder_album_or_event if used_album else "",
                "Notes": "No usable title text found" if status == "REVIEW" else "",
                "_rel_to_root": rel_to_root,
            })

    rows.sort(key=lambda r: (r["Category"], r["Subfolder"].lower(), r["Original Filename"].lower()))

    current_category = None
    for row in rows:
        if row["Category"] != current_category:
            current_category = row["Category"]
            count = sum(1 for r in rows if r["Category"] == current_category)
            log(f"\n=== {current_category} ({count}) ===", "header")

        location = f"{row['Subfolder']}/{row['Original Filename']}" if row["Subfolder"] else row["Original Filename"]
        context_note = _folder_context_note(row)
        if row["Status"] == "OK":
            log(f"  [OK]      {location}", "ok")
        elif row["Status"] == "NEEDS RENAME":
            log(f"  [NEEDS RENAME] {location}  ->  {row['New Name']}{context_note}", "renamed")
        elif row["Status"] == "REVIEW":
            log(f"  [REVIEW]  {location}  ({row['Notes']})", "review")
        else:
            log(f"  [OTHER]   {location}", "item")

    return rows


def _folder_context_note(row):
    parts = []
    if row.get("Artist From Folder"):
        parts.append(f"Artist: {row['Artist From Folder']}")
    if row.get("Album/Event From Folder"):
        parts.append(f"Album/Event: {row['Album/Event From Folder']}")
    return f"  (from folder — {', '.join(parts)})" if parts else ""


def write_report(rows, output_path):
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "Category", "Subfolder", "Status", "Original Filename", "New Name",
                "Artist From Folder", "Album/Event From Folder", "Notes",
            ],
            extrasaction="ignore",  # rows carry an internal "_rel_to_root" used only by apply_renames
        )
        writer.writeheader()
        writer.writerows(rows)


def apply_renames(root_folder, rows, output_folder, log):
    """
    Copy every file into output_folder using its New Name, mirroring the
    source's subfolder structure exactly. Originals are never touched —
    this only ever writes into the new output_folder.

    Two different source recordings can legitimately share the same New
    Name (e.g. the same bhajan sung on separate occasions — the sheet's
    convention is that ID3 tags distinguish those, not the filename), so
    the report never adds a disambiguating suffix. A single folder on
    disk still can't hold two files with the identical name, though — that
    part is only handled here, right at the copy, and only when a real
    collision actually happens; the reported New Name is left untouched.
    """
    applied = 0
    used_names_by_dir = {}
    for row in rows:
        rel_to_root = row.get("_rel_to_root", "")
        src_dir = os.path.join(root_folder, rel_to_root) if rel_to_root else root_folder
        dest_dir = os.path.join(output_folder, rel_to_root) if rel_to_root else output_folder
        os.makedirs(dest_dir, exist_ok=True)

        used_names = used_names_by_dir.setdefault(dest_dir, set())
        dest_filename = row["New Name"]
        if dest_filename.lower() in used_names:
            base_stem, base_ext = os.path.splitext(row["New Name"])
            suffix = 2
            while dest_filename.lower() in used_names:
                dest_filename = f"{base_stem} ({suffix}){base_ext}"
                suffix += 1
        used_names.add(dest_filename.lower())

        src_path = os.path.join(src_dir, row["Original Filename"])
        dest_path = os.path.join(dest_dir, dest_filename)
        shutil.copy2(src_path, dest_path)
        applied += 1

        location = f"{rel_to_root}/{row['Original Filename']}" if rel_to_root else row["Original Filename"]
        if dest_filename != row["Original Filename"]:
            note = "  (duplicate name, suffixed on disk)" if dest_filename != row["New Name"] else ""
            log(f"  {location}  ->  {dest_filename}{note}", "renamed")
        else:
            log(f"  {location}  (unchanged)", "ok")

    return applied
