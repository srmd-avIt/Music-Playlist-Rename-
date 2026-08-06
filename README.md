# Song Library Category Checker

Scans a music library folder (including subfolders), checks each file against
a naming-convention sheet, and proposes a corrected name for anything that
doesn't match — leading track numbers, glued-on duration codes, junk from
camera/WhatsApp exports, inconsistent casing, etc.

Read-only by default: scanning only produces an on-screen preview and a CSV
report. Nothing is renamed, copied, or moved until you explicitly apply
renames, and even then originals are copied (never modified) into a new
output folder.

Two interchangeable UIs share the same core logic in `song_naming_core.py`:

- **`list_songs_by_category.py`** — Tkinter desktop app.
  ```
  python3 list_songs_by_category.py
  ```
- **`streamlit_app.py`** — browser-based app.
  ```
  pip install -r requirements.txt
  streamlit run streamlit_app.py
  ```

## Features

- **Auto-detect**: classifies each file against 29 naming categories and
  fixes formatting/junk issues while deriving Artist/Album from the folder
  path where the filename itself doesn't have one.
- **Manual category**: force one category for every file in the folder
  instead of relying on auto-detect.
- **Detect from example**: type one filename that's already correctly named
  and the matching category is picked for you.
- **Custom rule from example**: type a before/after example of one file (e.g.
  `01 Song Title-30` → `Song Title`) and the same transformation — track
  numbers, duration codes, casing changes generalized — is learned and
  applied to every file in the folder, independent of the 29 categories.

`rename_checker.py` is an earlier draft of this tool, kept for reference.
