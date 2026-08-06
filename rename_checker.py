"""
Music file naming-convention checker.

- Pick a source folder (GUI folder dialog).
- Click Run.
- Every file in that folder is checked against the naming patterns from
  "Music Library Naming Convention_updated - Naming.csv".
- Every file is copied unchanged into a new output folder next to the
  source folder (originals are never touched or renamed automatically,
  since the correct Title/Artist/Album/etc. for a badly-named file can't
  be reliably guessed from the filename alone).
- A report CSV is written into the output folder listing, per file,
  whether it matched a known naming pattern ("OK") or needs manual
  renaming ("REVIEW"), plus which pattern it matched if any.
"""

import csv
import os
import re
import shutil
from datetime import datetime

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk
from tkinter import font as tkfont

AUDIO_EXTENSIONS = {
    ".mp3", ".wav", ".flac", ".aac", ".m4a", ".ogg", ".wma", ".aiff", ".alac",
}

# Segment that stops at a " - " boundary (used between dash-separated parts
# such as Title/Artist/Album/Event/Project/Lyricist).
D = r"(?:(?!\s-\s).)+"
# Segment used inside parentheses / before a trailing comma (Word, Lang,
# Type, Duration, Year, Original, OG Title, etc.) — just avoid the
# characters that bound it.
P = r"[^,()]+"

# (label, pattern) pairs built from the master naming-convention sheet.
# Several categories share the same structural shape (e.g. "Title - X - Y"
# covers Album Track / Event Recording / SRMD Project / Lyricist), so those
# are merged into one entry with a combined label.
## Ordered most-specific first: classify() returns the first match, so a
## keyword/paren-bearing pattern must be checked before the generic
## dash-only shapes it would otherwise also satisfy.
_RAW_PATTERNS = [
    ("SRMD Bhakti - No Album", rf"^{D} - {D}, SRMD Bhakti$"),
    ("SRMD Bhakti - With Album", rf"^{D} - {D} - {D}, SRMD Bhakti$"),
    ("SRMD Recording (Internal)", rf"^{D} - {D}, SRMD$"),
    ("SRMD Project-based (Title - Project - Artist, SRMD)",
     rf"^{D} - {D} - {D}, SRMD$"),
    ("SRMD Version - Same Title", rf"^{D} \(SRMD\) - {D}$"),
    ("SRMD Version - Different Title", rf"^{D} \({P} - SRMD\)\s*$"),
    ("Original (OG)", rf"^{D} \(OG\) - {D}$"),
    ("SRMD Version - Same Tune as OG", rf"^{D} \({P} Tune\), SRMD$"),
    ("Length Variant (Short/Extended)", rf"^{D} \((Short|Extended)\)$"),
    ("Loop - No Duration", rf"^{D} \(Loop\)\s*,\s*SRMD$"),
    ("Loop - With Duration", rf"^{D} \(Loop {P}\) - {D}$"),
    ("Dhun - Suffix Form", rf"^{D} \(Dhun\), SRMD$"),
    ("Dhun - Type Form", rf"^{D} \({P} Dhun\)(?: - {D})?$"),
    ("Instrumental", rf"^{D} \({P} Instru\), SRMD$"),
    ("Minus / Karaoke", rf"^{D} \(Minus\)\s*$"),
    ("Live Recording", rf"^{D} \(Live\) - {D}, SRMD$"),
    ("Language Variant", rf"^{D} \({P}\) - {D}, SRMD$"),
    ("Event-specific Edit", rf"^{D} \({P} Edit\)\s*(?:- {D})?$"),
    ("Words Removed (wo)", rf"^{D} \(wo {P}\) - {D}$"),
    ("Words Added (w)", rf"^{D} \(w {P}\) - {D}$"),
    ("Mangalacharan", rf"^Mangalacharan - {D} - {D}, SRMD$"),
    ("Arti - Standard (dash form)", rf"^Arti - {D} - {D} - {D}, SRMD$"),
    ("Arti - Standard (Title (Arti - Event) - Artist)",
     rf"^{D} \(Arti - {D}\) - {D}$"),
    ("Arti - Year-based (dash form)", rf"^Arti {P} - {D} - {D}, SRMD$"),
    ("Arti - Year-based (Title (Arti Year) - Artist)",
     rf"^{D} \(Arti {P}\) - {D}$"),
    ("Do Not Play / Restricted", rf"^! {D} - {D}(?:, SRMD Bhakti)?$"),
    ("Mashup", rf"^{D} x {D} - {D}$"),
    ("Event Recording / SRMD Project (Title (Event/Project) - Artist)",
     rf"^{D} \({P}\) - {D}$"),
    # Generic catch-alls last: they'd otherwise swallow every match above.
    ("Album Track / Event Recording / SRMD Project / Lyricist (Title - X - Y)",
     rf"^{D} - {D} - {D}$"),
    ("Singles With Artist (Title - Artist)", rf"^{D} - {D}$"),
    ("Singles - No Artist (Title only)", rf"^{D}$"),
]

# De-duplicate identical regex shapes while keeping the first (most
# descriptive) label.
_seen = set()
PATTERNS = []
for label, pattern in _RAW_PATTERNS:
    if pattern in _seen:
        continue
    _seen.add(pattern)
    PATTERNS.append((label, re.compile(pattern, re.IGNORECASE)))


# The sheet's "Title only" category has no real structure (a bare title is
# valid by definition), so it would otherwise accept any junk text as a
# "title". These hints catch common auto-generated / non-title filenames
# (phone recordings, camera dumps, export artifacts) so they still get
# flagged for review instead of silently passing.
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
    for label, regex in PATTERNS:
        if regex.match(name):
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


def _normalize_spacing(name):
    """Fix common formatting slips: underscores, and any dash used as a
    separator (double dashes, or no spaces at all, e.g. "Title-Artist")
    all become the standard " - "."""
    name = name.replace("_", " ")
    name = re.sub(r"\s*-+\s*", " - ", name)
    name = re.sub(r"\s*,\s*", ", ", name)
    name = re.sub(r"(?<=\S)\(", " (", name)  # "Em(SRMD)" -> "Em (SRMD)"
    name = re.sub(r"\s{2,}", " ", name)
    return name.strip(" -,")


def _normalize_casing(name):
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


def resolve_filename(name_no_ext):
    """
    Decide the output name for a file.

    Returns (final_name, status, matched_label):
      - status "OK"      -> name already matches a known pattern, unchanged
      - status "RENAMED" -> formatting was fixed / junk stripped to salvage a valid name
      - status "REVIEW"  -> no usable title text could be found; left unchanged
    """
    original = name_no_ext.strip()
    normalized = _normalize_casing(_normalize_spacing(original))

    # A junk indicator (WhatsApp export, camera dump, date/time stamp, ...)
    # anywhere in the name means it isn't safe to trust *any* structural
    # match on it — a pile of noise can accidentally line up with a
    # pattern's shape by chance. Route it to salvage instead. Checked on
    # the normalized text so underscore-joined junk (e.g. "final_v2") is
    # still caught — \b word boundaries don't fire against underscores.
    if not _JUNK_HINTS.search(normalized):
        label = classify(normalized)
        if label:
            status = "OK" if normalized == original else "RENAMED"
            return normalized, status, label

    salvaged = _strip_junk(normalized)
    if len(re.sub(r"[^A-Za-z]", "", salvaged)) >= 3:
        label = classify(salvaged)
        return salvaged, "RENAMED", label or "Singles - No Artist (Title only, auto-cleaned)"

    return original, "REVIEW", None


def process_folder(source_folder, log):
    files = [
        f for f in sorted(os.listdir(source_folder))
        if os.path.isfile(os.path.join(source_folder, f)) and not f.startswith(".")
    ]

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    parent = os.path.dirname(source_folder.rstrip(os.sep))
    base_name = os.path.basename(source_folder.rstrip(os.sep))
    output_folder = os.path.join(parent, f"{base_name} - Output {timestamp}")
    os.makedirs(output_folder, exist_ok=True)

    report_rows = []
    used_names = set()
    ok_count = 0
    renamed_count = 0
    review_count = 0

    for filename in files:
        name_no_ext, ext = os.path.splitext(filename)
        final_name, status, label = resolve_filename(name_no_ext)

        if status == "REVIEW":
            final_filename = filename
        else:
            candidate = final_name + ext
            suffix = 2
            final_filename = candidate
            while final_filename.lower() in used_names:
                final_filename = f"{final_name} ({suffix}){ext}"
                suffix += 1
        used_names.add(final_filename.lower())

        src_path = os.path.join(source_folder, filename)
        dst_path = os.path.join(output_folder, final_filename)
        shutil.copy2(src_path, dst_path)

        if status == "OK":
            ok_count += 1
            log(f"[OK]      {filename}  ->  {label}")
        elif status == "RENAMED":
            renamed_count += 1
            log(f"[RENAMED] {filename}  ->  {final_filename}   ({label})")
        else:
            review_count += 1
            log(f"[REVIEW]  {filename}  ->  no usable title text found, copied unchanged")

        report_rows.append({
            "Original Filename": filename,
            "Output Filename": final_filename,
            "Status": status,
            "Matched Pattern": label or "",
        })

    report_path = os.path.join(output_folder, "0_Naming_Review_Report.csv")
    with open(report_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f, fieldnames=["Original Filename", "Output Filename", "Status", "Matched Pattern"]
        )
        writer.writeheader()
        writer.writerows(report_rows)

    return output_folder, len(files), ok_count, renamed_count, review_count


BG = "#f4f5f7"
CARD_BG = "#ffffff"
BORDER = "#d9dce1"
TEXT_MUTED = "#6b7280"
ACCENT = "#2563eb"
GREEN = "#15803d"
AMBER = "#b45309"


class App:
    def __init__(self, root):
        self.root = root
        self.root.title("Music Filename Convention Checker")
        self.root.geometry("820x600")
        self.root.minsize(680, 480)
        self.root.configure(bg=BG)
        self.source_folder = tk.StringVar(value="No folder selected")

        self._init_style()

        outer = ttk.Frame(root, padding=16, style="App.TFrame")
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)

        ttk.Label(outer, text="Music Filename Convention Checker", style="Heading.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 4)
        )
        ttk.Label(
            outer,
            text="Select a folder, then Run. Files are copied (never modified) into a new output folder.",
            style="Subheading.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(0, 14))

        card = ttk.Frame(outer, style="Card.TFrame", padding=16)
        card.grid(row=2, column=0, sticky="nsew")
        card.columnconfigure(0, weight=1)
        card.rowconfigure(3, weight=1)

        picker = ttk.Frame(card, style="Card.TFrame")
        picker.grid(row=0, column=0, sticky="ew")
        picker.columnconfigure(1, weight=1)

        ttk.Label(picker, text="Source folder", style="FieldLabel.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 4)
        )
        self.folder_entry = ttk.Entry(picker, textvariable=self.source_folder, state="readonly")
        self.folder_entry.grid(row=1, column=0, sticky="ew", ipady=4)
        ttk.Button(picker, text="Browse…", command=self.browse).grid(
            row=1, column=1, padx=(8, 0)
        )

        actions = ttk.Frame(card, style="Card.TFrame")
        actions.grid(row=1, column=0, sticky="ew", pady=(14, 0))
        self.run_button = ttk.Button(
            actions, text="Run", command=self.run, state="disabled", style="Accent.TButton"
        )
        self.run_button.pack(side="left", ipadx=10, ipady=2)

        self.status_label = ttk.Label(
            card, text="Select a folder to begin.", style="Status.TLabel"
        )
        self.status_label.grid(row=2, column=0, sticky="w", pady=(10, 8))

        log_frame = ttk.Frame(card, style="LogBorder.TFrame")
        log_frame.grid(row=3, column=0, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

        mono_font = tkfont.Font(family="Menlo", size=11)
        self.log_box = scrolledtext.ScrolledText(
            log_frame,
            wrap="word",
            font=mono_font,
            bg="#1e1e1e",
            fg="#e5e7eb",
            insertbackground="#e5e7eb",
            relief="flat",
            borderwidth=0,
            padx=10,
            pady=8,
        )
        self.log_box.grid(row=0, column=0, sticky="nsew", padx=1, pady=1)
        self.log_box.tag_configure("ok", foreground="#4ade80")
        self.log_box.tag_configure("renamed", foreground="#60a5fa")
        self.log_box.tag_configure("review", foreground="#fbbf24")
        self.log_box.tag_configure("info", foreground="#9ca3af")
        self.log_box.configure(state="disabled")

    def _init_style(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use("aqua")
        except tk.TclError:
            pass

        style.configure("App.TFrame", background=BG)
        style.configure("Card.TFrame", background=CARD_BG)
        style.configure("LogBorder.TFrame", background=BORDER)
        style.configure(
            "Heading.TLabel", background=BG, font=("SF Pro Text", 18, "bold")
        )
        style.configure(
            "Subheading.TLabel", background=BG, foreground=TEXT_MUTED, font=("SF Pro Text", 12)
        )
        style.configure(
            "FieldLabel.TLabel",
            background=CARD_BG,
            foreground=TEXT_MUTED,
            font=("SF Pro Text", 11, "bold"),
        )
        style.configure(
            "Status.TLabel", background=CARD_BG, font=("SF Pro Text", 12)
        )
        style.configure("Accent.TButton", font=("SF Pro Text", 12, "bold"))

    def log(self, message, tag=None):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", message + "\n", tag)
        self.log_box.see("end")
        self.log_box.configure(state="disabled")
        self.root.update_idletasks()

    def browse(self):
        folder = filedialog.askdirectory(title="Select folder containing music files")
        if folder:
            self.source_folder.set(folder)
            self.run_button.configure(state="normal")
            self.status_label.configure(text=f"Ready to process: {folder}", foreground="black")

    def run(self):
        folder = self.source_folder.get()
        if not folder or not os.path.isdir(folder):
            messagebox.showerror("Error", "Please select a valid folder first.")
            return

        self.run_button.configure(state="disabled")
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")
        self.status_label.configure(text="Processing…", foreground=ACCENT)

        def log_with_tag(message):
            if message.startswith("[OK]"):
                tag = "ok"
            elif message.startswith("[RENAMED]"):
                tag = "renamed"
            elif message.startswith("[REVIEW]"):
                tag = "review"
            else:
                tag = "info"
            self.log(message, tag)

        try:
            output_folder, total, ok_count, renamed_count, review_count = process_folder(
                folder, log_with_tag
            )
        except Exception as exc:
            messagebox.showerror("Error", f"Something went wrong:\n{exc}")
            self.run_button.configure(state="normal")
            self.status_label.configure(text="Failed.", foreground="#b91c1c")
            return

        self.status_label.configure(
            text=(
                f"Done — {total} files processed, {ok_count} already OK, "
                f"{renamed_count} renamed, {review_count} need manual review."
            ),
            foreground=GREEN if review_count == 0 else AMBER,
        )
        self.run_button.configure(state="normal")
        messagebox.showinfo(
            "Done",
            f"Processed {total} files.\n"
            f"Already OK: {ok_count}\nAuto-renamed: {renamed_count}\n"
            f"Needs manual review: {review_count}\n\n"
            f"Output folder:\n{output_folder}\n\n"
            f"See 0_Naming_Review_Report.csv inside it for details.",
        )


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
