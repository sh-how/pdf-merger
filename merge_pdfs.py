#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = ["pypdf", "textual>=0.60"]
# ///
"""PDF Merger - a full-screen terminal app. Drag PDFs in, arrange them, press Enter.

Run:  python merge_pdfs.py      (installs what it needs on first run)
"""
import os
import shlex
import subprocess
import sys
from urllib.parse import unquote, urlparse

try:
    from pypdf import PdfReader, PdfWriter
    from textual import events
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical
    from textual.screen import ModalScreen
    from textual.widgets import Button, DataTable, Footer, Input, Label, Static
except ImportError:
    if os.environ.get("PDF_MERGER_SETUP"):
        sys.exit("Setup failed. Run:  pip install pypdf textual")
    print("First run: setting up (this takes a few seconds)...")
    base = [sys.executable, "-m", "pip", "install", "--quiet", "pypdf", "textual"]
    if subprocess.call(base) != 0 and subprocess.call(base + ["--user", "--break-system-packages"]) != 0:
        sys.exit("Setup failed. Run:  pip install pypdf textual")
    os.environ["PDF_MERGER_SETUP"] = "1"
    sys.exit(subprocess.call([sys.executable] + sys.argv))


def parse_dropped(text):
    """Turn whatever the terminal pasted on drag-and-drop into a list of paths."""
    paths = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("& "):  # PowerShell prefixes dropped paths with "& "
            line = line[2:].strip()
        if not line:
            continue
        whole = os.path.expanduser(line.strip("'\""))
        if os.path.isfile(whole):  # one path, possibly with unescaped spaces
            paths.append(whole)
            continue
        try:
            parts = shlex.split(line, posix=(os.name != "nt"))
        except ValueError:
            parts = [line]
        for p in parts:
            p = p.strip("'\"")
            if p.startswith("file://"):
                p = unquote(urlparse(p).path)
                if os.name == "nt":
                    p = p.lstrip("/")
            paths.append(os.path.expanduser(p))
    return paths


def free_name(path):
    """Never overwrite: merged.pdf -> merged (2).pdf -> merged (3).pdf ..."""
    stem, ext = os.path.splitext(path)
    n, out = 2, path
    while os.path.exists(out):
        out = f"{stem} ({n}){ext}"
        n += 1
    return out


class NameScreen(ModalScreen):
    """Asks what to call the merged file."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, folder):
        super().__init__()
        self.folder = folder

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Name the merged file", id="dialog-title")
            yield Input(value="merged.pdf", id="name")
            yield Label(f"Saves to {self.folder}", id="dialog-hint")
            with Horizontal(id="dialog-buttons"):
                yield Button("Merge", variant="success", id="ok")
                yield Button("Cancel", id="cancel")

    def on_mount(self):
        box = self.query_one(Input)
        box.focus()
        box.action_select_all() if hasattr(box, "action_select_all") else None

    def on_input_submitted(self, event):
        self.dismiss(event.value)

    def on_button_pressed(self, event):
        self.dismiss(self.query_one(Input).value if event.button.id == "ok" else None)

    def action_cancel(self):
        self.dismiss(None)


class PdfMerger(App):
    TITLE = "PDF Merger"
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    Screen { align: center top; }
    #main { width: 100%; max-width: 96; height: 100%; padding: 1 2; }
    #title { text-style: bold; color: $accent; }
    #subtitle { color: $text-muted; margin-bottom: 1; }
    #drop { height: 1fr; border: dashed $accent; content-align: center middle;
            text-align: center; color: $text-muted; }
    #table { height: 1fr; border: round $accent; }
    #status { height: auto; min-height: 1; margin-top: 1; }
    #buttons { height: 3; margin-top: 1; }
    #buttons Button { margin-right: 1; min-width: 12; }
    #merge { dock: right; margin-right: 0; }
    NameScreen { align: center middle; background: $background 60%; }
    #dialog { width: 64; height: auto; padding: 1 2; border: round $accent; background: $surface; }
    #dialog-title { text-style: bold; margin-bottom: 1; }
    #dialog-hint { color: $text-muted; margin: 1 0; }
    #dialog-buttons { height: 3; }
    #dialog-buttons Button { margin-right: 1; }
    """
    BINDINGS = [
        Binding("enter,m", "merge", "Merge", key_display="enter"),
        Binding("shift+up,u", "move(-1)", "Move up", key_display="shift+↑"),
        Binding("shift+down,d", "move(1)", "Move down", key_display="shift+↓"),
        Binding("delete,backspace,x", "remove", "Remove", key_display="del"),
        Binding("c", "clear", "Clear all"),
        Binding("o", "open_folder", "Open folder", show=False),
        Binding("q,escape", "quit", "Quit", key_display="q"),
    ]

    def __init__(self, initial=()):
        super().__init__()
        self.files = []  # (path, page count)
        self.initial = list(initial)
        self.last_saved = None

    def compose(self) -> ComposeResult:
        with Vertical(id="main"):
            yield Static("PDF Merger", id="title")
            yield Static("Combine PDFs into one file, top to bottom.", id="subtitle")
            yield Static(
                "[b]Drag PDF files into this window[/b]\n\n"
                "Drop one at a time or several together.\nThey are merged in the order shown.",
                id="drop",
            )
            yield DataTable(id="table", cursor_type="row", zebra_stripes=True)
            yield Static("", id="status")
            with Horizontal(id="buttons"):
                yield Button("↑ Up", id="up")
                yield Button("↓ Down", id="down")
                yield Button("Remove", id="remove", variant="error")
                yield Button("Clear all", id="clear")
                yield Button("Merge", id="merge", variant="success")
        yield Footer()

    def on_mount(self):
        self.query_one(DataTable).add_columns("#", "File", "Pages", "Folder")
        for button in self.query("#buttons Button"):
            button.can_focus = False  # keep arrow keys on the list after a click
        self.show(self.add(self.initial) if self.initial else "")

    # ---- drag and drop: terminals deliver dropped files as pasted paths ----
    def on_paste(self, event: events.Paste):
        if isinstance(self.screen, NameScreen):
            return
        self.last_saved = None
        self.show(self.add(parse_dropped(event.text)), cursor=len(self.files) - 1)

    def add(self, paths):
        added, skipped = 0, []
        for path in paths:
            try:
                if not os.path.isfile(path):
                    raise ValueError("file not found")
                self.files.append((os.path.abspath(path), len(PdfReader(path).pages)))
                added += 1
            except Exception as error:
                skipped.append(f"{os.path.basename(path) or path} ({error})")
        notes = []
        if added:
            notes.append(f"[green]Added {added} file{'s' if added != 1 else ''}.[/]")
        if skipped:
            notes.append("[red]Skipped: " + ", ".join(skipped) + "[/]")
        return " ".join(notes)

    def show(self, message="", cursor=None):
        table = self.query_one(DataTable)
        keep = table.cursor_row if cursor is None else cursor
        table.clear()
        for i, (path, pages) in enumerate(self.files, 1):
            table.add_row(str(i), os.path.basename(path), str(pages), self.short(os.path.dirname(path)))
        has = bool(self.files)
        table.display = has
        self.query_one("#drop").display = not has
        if has:
            table.move_cursor(row=max(0, min(keep, len(self.files) - 1)))
            table.focus()
        total = sum(p for _, p in self.files)
        summary = f"[dim]{len(self.files)} files, {total} pages. Drag in more any time.[/]" if has else ""
        self.query_one("#status").update(message or summary)
        for name in ("up", "down", "remove", "clear"):
            self.query_one(f"#{name}").disabled = not has
        self.query_one("#merge").disabled = len(self.files) < 2

    @staticmethod
    def short(folder):
        home = os.path.expanduser("~")
        return "~" + folder[len(home):] if folder.startswith(home) else folder

    # ---- actions ----
    def on_button_pressed(self, event):
        actions = {"up": lambda: self.action_move(-1), "down": lambda: self.action_move(1),
                   "remove": self.action_remove, "clear": self.action_clear, "merge": self.action_merge}
        if event.button.id in actions:
            actions[event.button.id]()

    def on_data_table_row_selected(self, event):
        self.action_merge()

    def action_move(self, step):
        row = self.query_one(DataTable).cursor_row
        target = row + step
        if self.files and 0 <= target < len(self.files):
            self.files[row], self.files[target] = self.files[target], self.files[row]
            self.show(cursor=target)

    def action_remove(self):
        if self.files:
            row = self.query_one(DataTable).cursor_row
            name = os.path.basename(self.files.pop(row)[0])
            self.show(f"Removed {name}.", cursor=row)

    def action_clear(self):
        self.files = []
        self.show()

    def action_merge(self):
        if len(self.files) < 2:
            self.show("[yellow]Add at least two PDFs to merge.[/]")
            return
        folder = os.path.dirname(self.files[0][0])
        self.push_screen(NameScreen(folder), lambda name: self.finish(folder, name))

    def finish(self, folder, name):
        if name is None:
            return
        name = os.path.basename(name.strip().strip("'\"")) or "merged.pdf"
        if not name.lower().endswith(".pdf"):
            name += ".pdf"
        out = free_name(os.path.join(folder, name))
        try:
            writer = PdfWriter()
            for path, _ in self.files:
                for page in PdfReader(path).pages:
                    writer.add_page(page)
            with open(out, "wb") as handle:
                writer.write(handle)
        except Exception as error:
            self.show(f"[red]Merge failed: {error}[/]")
            return
        pages = len(writer.pages)
        self.files, self.last_saved = [], out
        self.show(f"[green]✓ Saved {pages} pages to {self.short(out)}[/]\n[dim]Press o to open its folder, or drag in more PDFs.[/]")

    def action_open_folder(self):
        if not self.last_saved:
            return
        folder = os.path.dirname(self.last_saved)
        try:
            if os.name == "nt":
                os.startfile(folder)
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", folder],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            self.show(f"Folder: {folder}")


if __name__ == "__main__":
    PdfMerger(sys.argv[1:]).run()
