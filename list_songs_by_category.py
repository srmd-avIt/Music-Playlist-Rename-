"""
Tkinter desktop UI for scanning a music library and working out what each
file's name should be per the naming-convention sheet. All the actual
scanning/renaming logic lives in song_naming_core.py - this file is just
the GUI built on top of it (see streamlit_app.py for the browser-based
equivalent).

Read-only: nothing on disk is renamed, copied, or moved. A CSV report
lists every file's category, status, and the proposed New Name.
"""

import os
from datetime import datetime

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk
from tkinter import font as tkfont

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
    write_report,
)

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
        self.root.title("Song Library Category Checker")
        self.root.geometry("880x620")
        self.root.minsize(700, 480)
        self.root.configure(bg=BG)
        self.source_folder = tk.StringVar(value="No folder selected")

        self._init_style()

        outer = ttk.Frame(root, padding=16, style="App.TFrame")
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)

        ttk.Label(outer, text="Song Library Category Checker", style="Heading.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 4)
        )
        ttk.Label(
            outer,
            text=(
                "Select the main folder (subfolders included). Read-only — nothing on disk is "
                "renamed or moved. A CSV report is saved with a New Name column proposing the "
                "corrected name for each file that needs one."
            ),
            style="Subheading.TLabel",
            wraplength=760,
        ).grid(row=1, column=0, sticky="w", pady=(0, 14))

        card = ttk.Frame(outer, style="Card.TFrame", padding=16)
        card.grid(row=2, column=0, sticky="nsew")
        card.columnconfigure(0, weight=1)
        card.rowconfigure(7, weight=1)

        picker = ttk.Frame(card, style="Card.TFrame")
        picker.grid(row=0, column=0, sticky="ew")
        picker.columnconfigure(1, weight=1)

        ttk.Label(picker, text="Main folder", style="FieldLabel.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 4)
        )
        self.folder_entry = ttk.Entry(picker, textvariable=self.source_folder, state="readonly")
        self.folder_entry.grid(row=1, column=0, sticky="ew", ipady=4)
        ttk.Button(picker, text="Browse…", command=self.browse).grid(row=1, column=1, padx=(8, 0))

        # Category override: "Auto-detect" (default) reproduces the classify
        # + resolve_filename behavior; picking a specific category forces
        # that pattern for every file instead, via the dynamic fields below.
        category_row = ttk.Frame(card, style="Card.TFrame")
        category_row.grid(row=1, column=0, sticky="ew", pady=(14, 0))
        category_row.columnconfigure(1, weight=1)

        ttk.Label(category_row, text="Category", style="FieldLabel.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 4)
        )
        self._category_by_label = {AUTO_DETECT_LABEL: None}
        self._display_by_category_name = {}
        combo_values = [AUTO_DETECT_LABEL]
        for cat in CATEGORY_PATTERNS:
            display = f"{cat['name']}  →  {cat['pattern_display']}"
            self._category_by_label[display] = cat
            self._display_by_category_name[cat["name"]] = display
            combo_values.append(display)

        self.category_var = tk.StringVar(value=AUTO_DETECT_LABEL)
        self.category_combo = ttk.Combobox(
            category_row, textvariable=self.category_var, values=combo_values, state="readonly"
        )
        self.category_combo.grid(row=1, column=0, sticky="ew")
        self.category_combo.bind("<<ComboboxSelected>>", self._on_category_change)

        self.include_subfolders_var = tk.BooleanVar(value=True)
        self.include_subfolders_check = ttk.Checkbutton(
            category_row, text="Include subfolders", variable=self.include_subfolders_var
        )
        self.include_subfolders_check.grid(row=1, column=1, padx=(8, 0))

        # Lets you type one example filename that already has the naming
        # right, instead of hunting through the Category dropdown above for
        # the matching entry - useful when auto-detect keeps picking the
        # wrong category for a batch of files that all share one format.
        sample_row = ttk.Frame(card, style="Card.TFrame")
        sample_row.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        sample_row.columnconfigure(1, weight=1)

        ttk.Label(sample_row, text="Or detect pattern from an example filename", style="FieldLabel.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 4)
        )
        self.sample_var = tk.StringVar()
        self.sample_entry = ttk.Entry(sample_row, textvariable=self.sample_var)
        self.sample_entry.grid(row=1, column=0, sticky="ew", ipady=4)
        self.sample_entry.bind("<Return>", lambda e: self.detect_from_sample())
        ttk.Button(sample_row, text="Detect", command=self.detect_from_sample).grid(row=1, column=1, padx=(8, 0))

        # Custom rule: learn a rename from one before/after example instead
        # of picking any of the 29 categories at all - for naming quirks
        # (a stray track number, a duration code with no fixed shape) that
        # aren't part of the naming-convention sheet. Filling both boxes
        # overrides the Category dropdown above when Run is clicked.
        custom_row = ttk.Frame(card, style="Card.TFrame")
        custom_row.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        custom_row.columnconfigure(0, weight=1)
        custom_row.columnconfigure(1, weight=1)

        ttk.Label(
            custom_row,
            text="Or teach a custom rule: current name of one file, and what you want it renamed to",
            style="FieldLabel.TLabel",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))
        self.custom_before_var = tk.StringVar()
        self.custom_after_var = tk.StringVar()
        ttk.Entry(custom_row, textvariable=self.custom_before_var).grid(
            row=1, column=0, sticky="ew", ipady=4, padx=(0, 4)
        )
        ttk.Entry(custom_row, textvariable=self.custom_after_var).grid(
            row=1, column=1, sticky="ew", ipady=4, padx=(4, 0)
        )
        ttk.Label(custom_row, text="Current name", style="Subheading.TLabel").grid(
            row=2, column=0, sticky="w", pady=(2, 0)
        )
        ttk.Label(custom_row, text="Desired name", style="Subheading.TLabel").grid(
            row=2, column=1, sticky="w", pady=(2, 0)
        )

        self.fields_frame = ttk.Frame(card, style="Card.TFrame")
        self.fields_frame.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        self.field_vars = {}
        self.field_hint_label = ttk.Label(
            self.fields_frame,
            text="",
            style="Subheading.TLabel",
            wraplength=760,
        )

        actions = ttk.Frame(card, style="Card.TFrame")
        actions.grid(row=5, column=0, sticky="ew", pady=(14, 0))
        self.run_button = ttk.Button(
            actions, text="Run", command=self.run, state="disabled", style="Accent.TButton"
        )
        self.run_button.pack(side="left", ipadx=10, ipady=2)
        self.apply_button = ttk.Button(
            actions, text="Apply Renames…", command=self.apply, state="disabled"
        )
        self.apply_button.pack(side="left", padx=(8, 0), ipadx=10, ipady=2)

        self._last_rows = None
        self._last_root = None

        self.status_label = ttk.Label(card, text="Select a folder to begin.", style="Status.TLabel")
        self.status_label.grid(row=6, column=0, sticky="w", pady=(10, 8))

        log_frame = ttk.Frame(card, style="LogBorder.TFrame")
        log_frame.grid(row=7, column=0, sticky="nsew")
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
        self.log_box.tag_configure("header", foreground="#93c5fd", font=(mono_font.actual("family"), 11, "bold"))
        self.log_box.tag_configure("item", foreground="#e5e7eb")
        self.log_box.tag_configure("ok", foreground="#4ade80")
        self.log_box.tag_configure("renamed", foreground="#60a5fa")
        self.log_box.tag_configure("review", foreground="#fbbf24")
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
        style.configure("Heading.TLabel", background=BG, font=("SF Pro Text", 18, "bold"))
        style.configure("Subheading.TLabel", background=BG, foreground=TEXT_MUTED, font=("SF Pro Text", 12))
        style.configure(
            "FieldLabel.TLabel", background=CARD_BG, foreground=TEXT_MUTED, font=("SF Pro Text", 11, "bold")
        )
        style.configure("Status.TLabel", background=CARD_BG, font=("SF Pro Text", 12))
        style.configure("Accent.TButton", font=("SF Pro Text", 12, "bold"))

    def log(self, message, tag=None):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", message + "\n", tag)
        self.log_box.see("end")
        self.log_box.configure(state="disabled")
        self.root.update_idletasks()

    def browse(self):
        folder = filedialog.askdirectory(title="Select main folder to scan")
        if folder:
            self.source_folder.set(folder)
            self.run_button.configure(state="normal")
            self.apply_button.configure(state="disabled")
            self._last_rows = None
            self._last_root = None
            self.status_label.configure(text=f"Ready to scan: {folder}", foreground="black")

    def _on_category_change(self, event=None):
        for child in self.fields_frame.winfo_children():
            child.grid_forget()
        self.field_vars = {}

        category = self._category_by_label.get(self.category_var.get())
        if category is None:
            # Auto-detect: no extra fields to collect.
            return

        for i, field in enumerate(category["fields"]):
            var = tk.StringVar()
            self.field_vars[field] = var
            ttk.Label(self.fields_frame, text=FIELD_LABELS.get(field, field), style="FieldLabel.TLabel").grid(
                row=0, column=i * 2, sticky="w", padx=(0 if i == 0 else 12, 4)
            )
            entry = ttk.Entry(self.fields_frame, textvariable=var, width=22)
            entry.grid(row=1, column=i * 2, sticky="w", padx=(0 if i == 0 else 12, 4), ipady=3)

        if "Artist" in category["fields"]:
            self.field_hint_label.configure(
                text="Leave Artist blank to use the folder-detected artist (e.g. \"Bhakti With X\") per file."
            )
            self.field_hint_label.grid(row=2, column=0, columnspan=len(category["fields"]) * 2, sticky="w", pady=(6, 0))
        else:
            self.field_hint_label.grid_forget()

    def _select_category(self, category):
        """Set the Category dropdown to `category` and rebuild its fields,
        as if the user had picked it by hand."""
        self.category_var.set(self._display_by_category_name[category["name"]])
        self._on_category_change()

    def detect_from_sample(self):
        """
        Detect which manual category an example filename belongs to (typed
        into the "detect pattern from example" box) and select it in the
        Category dropdown, instead of the user having to find it themselves
        among the 29 entries. Some pattern shapes fit more than one
        category (see detect_category_candidates) - those prompt a small
        chooser dialog rather than guessing.
        """
        example = self.sample_var.get().strip()
        if not example:
            messagebox.showerror("Error", "Type an example filename first.")
            return

        candidates, error = detect_category_candidates(example)
        if error:
            messagebox.showerror("No match", error)
            return

        if len(candidates) == 1:
            self._select_category(candidates[0])
            self.status_label.configure(
                text=f"Detected category: {candidates[0]['name']}", foreground=GREEN
            )
            return

        self._choose_category_dialog(candidates)

    def _choose_category_dialog(self, candidates):
        """Small modal listing pattern shapes that fit more than one
        category, so the user picks the intended one instead of us
        guessing which one they meant."""
        dialog = tk.Toplevel(self.root)
        dialog.title("Which category did you mean?")
        dialog.configure(bg=CARD_BG)
        dialog.transient(self.root)
        dialog.grab_set()

        ttk.Label(
            dialog,
            text="That example matches more than one category. Which one did you mean?",
            style="FieldLabel.TLabel",
            wraplength=380,
            background=CARD_BG,
        ).pack(padx=16, pady=(16, 8), anchor="w")

        for cat in candidates:
            ttk.Button(
                dialog,
                text=f"{cat['name']}  →  {cat['pattern_display']}",
                command=lambda c=cat: (self._select_category(c), dialog.destroy()),
            ).pack(fill="x", padx=16, pady=4)

        ttk.Button(dialog, text="Cancel", command=dialog.destroy).pack(padx=16, pady=(8, 16))

    def run(self):
        folder = self.source_folder.get()
        if not folder or not os.path.isdir(folder):
            messagebox.showerror("Error", "Please select a valid folder first.")
            return

        custom_before = self.custom_before_var.get().strip()
        custom_after = self.custom_after_var.get().strip()
        use_custom_rule = bool(custom_before and custom_after)

        category = None
        if not use_custom_rule:
            category = self._category_by_label.get(self.category_var.get())
            if category is not None:
                missing_inputs = [
                    FIELD_LABELS.get(f, f) for f in category["fields"]
                    if f != "Artist" and not self.field_vars.get(f, tk.StringVar()).get().strip()
                ]
                if missing_inputs:
                    messagebox.showerror(
                        "Missing fields",
                        f"This category needs: {', '.join(missing_inputs)}.\n"
                        f"(Artist may be left blank if the folder name provides it.)",
                    )
                    return

        self.run_button.configure(state="disabled")
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")
        self.status_label.configure(text="Scanning…", foreground=ACCENT)

        try:
            if use_custom_rule:
                try:
                    apply_fn = build_custom_rule(custom_before, custom_after)
                except ValueError as exc:
                    messagebox.showerror("Can't learn rule", str(exc))
                    self.run_button.configure(state="normal")
                    self.status_label.configure(text="Failed.", foreground="#b91c1c")
                    return
                rows = process_library_custom_rule(
                    folder, self.log, apply_fn, self.include_subfolders_var.get()
                )
            elif category is None:
                rows = process_library(folder, self.log)
            else:
                field_values = {f: v.get().strip() for f, v in self.field_vars.items()}
                rows = process_library_manual(
                    folder, self.log, category, field_values, self.include_subfolders_var.get()
                )
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            report_path = os.path.join(folder, f"Song_List_By_Category_{timestamp}.csv")
            write_report(rows, report_path)
        except Exception as exc:
            messagebox.showerror("Error", f"Something went wrong:\n{exc}")
            self.run_button.configure(state="normal")
            self.status_label.configure(text="Failed.", foreground="#b91c1c")
            return

        self._last_rows = rows
        self._last_root = folder

        categories = sorted(set(r["Category"] for r in rows))
        ok_count = sum(1 for r in rows if r["Status"] == "OK")
        renamed_count = sum(1 for r in rows if r["Status"] == "NEEDS RENAME")
        review_count = sum(1 for r in rows if r["Status"] == "REVIEW")
        self.status_label.configure(
            text=(
                f"Done — {len(rows)} files across {len(categories)} categories: "
                f"{ok_count} OK, {renamed_count} need renaming, {review_count} need review."
            ),
            foreground=GREEN if review_count == 0 else AMBER,
        )
        self.run_button.configure(state="normal")
        self.apply_button.configure(state="normal")
        messagebox.showinfo(
            "Done",
            f"Found {len(rows)} files across {len(categories)} categories.\n\n"
            f"Already OK: {ok_count}\nSuggested rename: {renamed_count}\n"
            f"Needs manual review: {review_count}\n\n"
            f"Report saved to:\n{report_path}\n\n"
            f"Nothing was renamed or moved on disk — review the New Name column, "
            f"then click \"Apply Renames…\" when you're ready to create the renamed copies.",
        )

    def apply(self):
        if not self._last_rows or not self._last_root:
            messagebox.showerror("Error", "Run a scan first so there's something to apply.")
            return

        total = len(self._last_rows)
        changed = sum(1 for r in self._last_rows if r["New Name"] != r["Original Filename"])
        confirmed = messagebox.askyesno(
            "Apply renames?",
            f"This copies all {total} files (including {changed} being renamed) into a new "
            f"output folder next to the one you scanned. Your original files are never modified.\n\n"
            f"Continue?",
        )
        if not confirmed:
            return

        self.run_button.configure(state="disabled")
        self.apply_button.configure(state="disabled")
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")
        self.status_label.configure(text="Applying renames…", foreground=ACCENT)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        root_name = os.path.basename(self._last_root.rstrip(os.sep))
        parent = os.path.dirname(self._last_root.rstrip(os.sep))
        output_folder = os.path.join(parent, f"{root_name} - Renamed Output {timestamp}")

        try:
            applied = apply_renames(self._last_root, self._last_rows, output_folder, self.log)
        except Exception as exc:
            messagebox.showerror("Error", f"Something went wrong:\n{exc}")
            self.run_button.configure(state="normal")
            self.apply_button.configure(state="normal")
            self.status_label.configure(text="Failed.", foreground="#b91c1c")
            return

        self.status_label.configure(text=f"Done — {applied} files written to output folder.", foreground=GREEN)
        self.run_button.configure(state="normal")
        self.apply_button.configure(state="normal")
        messagebox.showinfo(
            "Done",
            f"Copied {applied} files to:\n{output_folder}\n\nYour originals were not modified.",
        )


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
