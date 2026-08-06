"""
Streamlit UI for scanning a music library and working out what each file's
name should be per the naming-convention sheet. Same core logic as the
Tkinter desktop app (list_songs_by_category.py) - see song_naming_core.py
for the actual scanning/renaming logic; this file is just the browser UI.

Run with: streamlit run streamlit_app.py

Read-only until "Apply Renames" is clicked: scanning only computes a CSV
report and an on-screen preview, nothing is renamed/copied/moved on disk.
"""

import csv
import io
import os
from datetime import datetime

import pandas as pd
import streamlit as st

from song_naming_core import (
    AUTO_DETECT_LABEL,
    CATEGORY_PATTERNS,
    FIELD_LABELS,
    apply_renames,
    build_custom_rule,
    detect_category_candidates,
    process_library,
    process_library_custom_rule,
    process_library_manual,
)

REPORT_FIELDNAMES = [
    "Category", "Subfolder", "Status", "Original Filename", "New Name",
    "Artist From Folder", "Album/Event From Folder", "Notes",
]


def rows_to_csv_bytes(rows):
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=REPORT_FIELDNAMES, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8")


st.set_page_config(page_title="Song Library Category Checker", layout="wide")
st.title("Song Library Category Checker")
st.caption(
    "Select the main folder (subfolders included). Read-only — nothing on disk is renamed or "
    "moved until you click \"Apply Renames\". A CSV report lists the proposed New Name for "
    "every file that needs one."
)

# --- Category display <-> data lookups (shared by the selectbox and the
# "detect from example" / custom-rule flows below) ---------------------
category_by_label = {AUTO_DETECT_LABEL: None}
display_by_name = {}
category_options = [AUTO_DETECT_LABEL]
for cat in CATEGORY_PATTERNS:
    display = f"{cat['name']}  →  {cat['pattern_display']}"
    category_by_label[display] = cat
    display_by_name[cat["name"]] = display
    category_options.append(display)

st.subheader("1. Folder to scan")
folder = st.text_input(
    "Main folder path",
    key="folder_path",
    placeholder="/path/to/your/music/folder",
    help="Paste the full path. On macOS: right-click the folder → hold Option → \"Copy as Pathname\".",
)

st.subheader("2. How to name files")

# Detect-from-example runs BEFORE the Category selectbox is instantiated,
# so it's safe for it to update the selectbox's session-state value this
# same script run (Streamlit forbids writing to a widget's session-state
# key after that widget has already been created in the current run).
with st.expander("Detect pattern from an example filename"):
    example = st.text_input(
        "One filename that already has the naming right",
        key="detect_example",
    )
    if st.button("Detect"):
        if not example.strip():
            st.error("Type an example filename first.")
        else:
            candidates, error = detect_category_candidates(example)
            if error:
                st.error(error)
                st.session_state["_detect_candidates"] = None
            elif len(candidates) == 1:
                st.session_state["category_label"] = display_by_name[candidates[0]["name"]]
                st.session_state["_detect_candidates"] = None
                st.success(f"Detected category: {candidates[0]['name']}")
            else:
                st.session_state["_detect_candidates"] = candidates

    pending_candidates = st.session_state.get("_detect_candidates")
    if pending_candidates:
        st.write("That example matches more than one category — which did you mean?")
        for cat in pending_candidates:
            if st.button(f"{cat['name']}  →  {cat['pattern_display']}", key=f"pick_{cat['name']}"):
                st.session_state["category_label"] = display_by_name[cat["name"]]
                st.session_state["_detect_candidates"] = None
                st.rerun()

col_cat, col_sub = st.columns([4, 1])
with col_cat:
    selected_label = st.selectbox("Category", category_options, key="category_label")
with col_sub:
    st.write("")
    include_subfolders = st.checkbox("Include subfolders", value=True, key="include_subfolders")

category = category_by_label[selected_label]
field_values = {}
if category is not None and category["fields"]:
    cols = st.columns(len(category["fields"]))
    for col, field in zip(cols, category["fields"]):
        with col:
            field_values[field] = st.text_input(
                FIELD_LABELS.get(field, field), key=f"field_{field}"
            )
    if "Artist" in category["fields"]:
        st.caption(
            "Leave Artist blank to use the folder-detected artist (e.g. \"Bhakti With X\") per file."
        )

with st.expander("Or teach a custom rule from one before/after example (overrides Category above)"):
    c1, c2 = st.columns(2)
    with c1:
        custom_before = st.text_input("Current name of one file", key="custom_before")
    with c2:
        custom_after = st.text_input("What you want it renamed to", key="custom_after")
    st.caption(
        "Text common to both is kept per-file (e.g. the song title); text removed is dropped "
        "(numbers inside it generalize to any number); text added is inserted for every file."
    )

st.subheader("3. Run")
run_clicked = st.button("Run", type="primary")

if run_clicked:
    if not folder or not os.path.isdir(folder):
        st.error("Please enter a valid folder path first.")
    else:
        use_custom_rule = bool(custom_before.strip() and custom_after.strip())

        missing_inputs = []
        if not use_custom_rule and category is not None:
            missing_inputs = [
                FIELD_LABELS.get(f, f) for f in category["fields"]
                if f != "Artist" and not field_values.get(f, "").strip()
            ]

        if missing_inputs:
            st.error(
                f"This category needs: {', '.join(missing_inputs)}. "
                f"(Artist may be left blank if the folder name provides it.)"
            )
        else:
            log_lines = []

            def log(message, tag=None):
                log_lines.append(message)

            try:
                if use_custom_rule:
                    apply_fn = build_custom_rule(custom_before, custom_after)
                    rows = process_library_custom_rule(folder, log, apply_fn, include_subfolders)
                elif category is None:
                    rows = process_library(folder, log)
                else:
                    rows = process_library_manual(
                        folder, log, category, field_values, include_subfolders
                    )
            except ValueError as exc:
                st.error(f"Can't learn rule: {exc}")
                rows = None
            except Exception as exc:
                st.error(f"Something went wrong: {exc}")
                rows = None

            if rows is not None:
                st.session_state["rows"] = rows
                st.session_state["last_root"] = folder
                st.session_state["log_lines"] = log_lines
                st.session_state["confirm_apply"] = False

rows = st.session_state.get("rows")
if rows:
    df = pd.DataFrame(rows).drop(columns=["_rel_to_root"], errors="ignore")
    ok_count = sum(1 for r in rows if r["Status"] == "OK")
    renamed_count = sum(1 for r in rows if r["Status"] == "NEEDS RENAME")
    review_count = sum(1 for r in rows if r["Status"] == "REVIEW")
    categories = sorted(set(r["Category"] for r in rows))

    st.success(
        f"Found {len(rows)} files across {len(categories)} categories — "
        f"{ok_count} OK, {renamed_count} need renaming, {review_count} need review."
    )
    st.dataframe(df, width="stretch", height=420)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    st.download_button(
        "Download CSV report",
        data=rows_to_csv_bytes(rows),
        file_name=f"Song_List_By_Category_{timestamp}.csv",
        mime="text/csv",
    )

    with st.expander("Detailed log"):
        st.code("\n".join(st.session_state.get("log_lines", [])) or "(nothing to show)")

    st.subheader("4. Apply renames")
    total = len(rows)
    changed = sum(1 for r in rows if r["New Name"] != r["Original Filename"])
    st.write(
        f"This copies all {total} files (including {changed} being renamed) into a new output "
        f"folder next to the one scanned. Your original files are never modified."
    )
    confirm = st.checkbox("I understand — create the renamed copies", key="confirm_apply")
    if st.button("Apply Renames", disabled=not confirm):
        last_root = st.session_state["last_root"]
        apply_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        root_name = os.path.basename(last_root.rstrip(os.sep))
        parent = os.path.dirname(last_root.rstrip(os.sep))
        output_folder = os.path.join(parent, f"{root_name} - Renamed Output {apply_timestamp}")

        apply_log_lines = []

        def apply_log(message, tag=None):
            apply_log_lines.append(message)

        try:
            applied = apply_renames(last_root, rows, output_folder, apply_log)
        except Exception as exc:
            st.error(f"Something went wrong: {exc}")
        else:
            st.success(f"Copied {applied} files to:\n\n{output_folder}\n\nYour originals were not modified.")
            with st.expander("Apply log"):
                st.code("\n".join(apply_log_lines))
