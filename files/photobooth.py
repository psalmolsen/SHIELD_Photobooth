"""Photobooth strip automation: watch folder -> 4 photos -> template -> forPrint folder."""
import json, os, queue, sys, time, threading
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

APP_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
CONFIG = APP_DIR / "config.json"
DEFAULTS = {
    "watch_folder": str(Path.home() / "Pictures" / "Camera Roll"),
    "templates_folder": str(APP_DIR / "templates"),
    "output_folder": str(APP_DIR / "forPrint"),
    "printed_folder": str(APP_DIR / "Printed"),
    "printer": "",                 # empty = current Windows default printer
    "auto_print": True,
    "paper": "4x6_two_strips",     # 4x6_one_strip | 4x6_two_strips | a4
    "dpi": 300,
}
PAGES = {"4x6": (1200, 1800), "a4": (2480, 3508)}  # pixels at 300 DPI
PHOTOS_NEEDED = 4


def load_config():
    cfg = dict(DEFAULTS)
    if CONFIG.exists():
        cfg.update(json.loads(CONFIG.read_text()))
    old_output = str(Path.home() / "Documents" / "PhotoboothOutput")
    if cfg["output_folder"] == old_output or not str(cfg["output_folder"]).endswith("forPrint"):
        cfg["output_folder"] = DEFAULTS["output_folder"]
    cfg.setdefault("printed_folder", DEFAULTS["printed_folder"])
    cfg.setdefault("printer", "")
    cfg.setdefault("auto_print", True)
    ensure_output_folders(cfg)
    return cfg


def save_config(cfg):
    CONFIG.write_text(json.dumps(cfg, indent=2))


def ensure_output_folders(cfg):
    """Create the waiting and completed print folders beside the app."""
    Path(cfg["output_folder"]).mkdir(parents=True, exist_ok=True)
    Path(cfg["printed_folder"]).mkdir(parents=True, exist_ok=True)


# ---------- templates & slots ----------
def _runs(flags):
    out, start = [], None
    for i, f in enumerate(flags):
        if f and start is None:
            start = i
        elif not f and start is not None:
            out.append((start, i)); start = None
    if start is not None:
        out.append((start, len(flags)))
    return out


def key_mask(arr):
    """Pure magenta (#FF00FF) marker pixels."""
    r, g, b, al = (arr[..., i].astype(int) for i in range(4))
    return (r > 200) & (g < 100) & (b > 200) & (al > 200)


def detect_slots(img):
    """Find photo slots: transparent holes, or else magenta (#FF00FF) boxes.
    Returns [(x, y, w, h)] column by column, top to bottom."""
    arr = np.array(img.convert("RGBA"))
    mask = arr[..., 3] < 128
    if not mask.any():
        mask = key_mask(arr)
    boxes = []
    for c0, c1 in _runs(mask.any(0)):
        for r0, r1 in _runs(mask[:, c0:c1].any(1)):
            cols = np.where(mask[r0:r1, c0:c1].any(0))[0]
            x0, x1 = c0 + cols[0], c0 + cols[-1] + 1
            w, h = x1 - x0, r1 - r0
            if w >= 100 and h >= 100 and w * h < 0.5 * img.width * img.height:
                boxes.append((int(x0), int(r0), int(w), int(h)))
    return boxes


def _warm(arr, rb, gap):
    r, g, b = (arr[..., i].astype(int) for i in range(3))
    return (r - b > rb) & (r > g + gap)


def find_windows(arr):
    """Placeholder-filled templates: the photo windows are the areas fully enclosed by the
    warm-coloured (bamboo/flower) frame. Returns a boolean mask of the replaceable pixels."""
    from scipy import ndimage
    free = ~_warm(arr, 30, 8)
    lab, n = ndimage.label(free)
    edge = set(np.unique(np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1]])))
    sizes = np.bincount(lab.ravel())
    holes = np.zeros(free.shape, bool)
    for k in range(1, n + 1):
        if k not in edge and sizes[k] >= 20000:
            holes |= lab == k
    return ndimage.binary_dilation(holes, iterations=2) & ~_warm(arr, 50, 15)


def prepare_template(path):
    """Return a copy of the template with transparent photo windows (cached in templates/prepared)."""
    path = Path(path)
    out = path.parent / "prepared" / f"{path.stem}.png"
    if out.exists() and out.stat().st_mtime >= path.stat().st_mtime:
        return out
    arr = np.array(Image.open(path).convert("RGBA"))
    if (arr[..., 3] < 128).any():
        holes = None                       # already has transparent windows
    else:
        store = path.parent / "slots.json"
        data = json.loads(store.read_text()) if store.exists() else {}
        slots = data.get(path.name, [])
        if slots:
            holes = np.zeros(arr.shape[:2], dtype=bool)
            for x, y, w, h in slots:
                holes[y:y + h, x:x + w] = True
        else:
            holes = key_mask(arr)          # magenta markers
        if not holes.any():
            holes = find_windows(arr)      # placeholder pictures inside frames
        if not holes.any():
            raise ValueError(f"{path.name}: could not find the photo windows.")
        arr[holes, 3] = 0
    out.parent.mkdir(exist_ok=True)
    Image.fromarray(arr).save(out)
    return out


def get_slots(template_path):
    """Slots come from slots.json (editable); auto-detected and saved on first use."""
    path = Path(template_path)
    store = path.parent / "slots.json"
    data = json.loads(store.read_text()) if store.exists() else {}
    if path.name not in data:
        slots = detect_slots(Image.open(prepare_template(path)))
        data[path.name] = [list(s) for s in slots]
        store.write_text(json.dumps(data, indent=2))
    return [tuple(s) for s in data[path.name]]


# ---------- compositing ----------
def compose(template_path, photo_paths):
    """Slots may be a multiple of the photo count (e.g. 2 strips on one sheet):
    photo i goes into every slot where slot_index % photo_count == i."""
    slots = get_slots(template_path)
    tpl = Image.open(prepare_template(template_path)).convert("RGBA")
    n = len(photo_paths)
    if not slots or len(slots) % n:
        raise ValueError(f"{Path(template_path).name}: found {len(slots)} photo slots, "
                         f"expected {n} (or a multiple). Check slots.json / magenta boxes.")
    photos = [ImageOps.exif_transpose(Image.open(p)).convert("RGB") for p in photo_paths]
    canvas = Image.new("RGBA", tpl.size, (255, 255, 255, 255))
    arr = np.array(tpl)
    g = 2  # small bleed so no white edge shows under the frame
    for i, (x, y, w, h) in enumerate(slots):
        x0, y0 = max(x - g, 0), max(y - g, 0)
        x1, y1 = min(x + w + g, tpl.width), min(y + h + g, tpl.height)
        fit = ImageOps.fit(photos[i % n], (x1 - x0, y1 - y0), Image.LANCZOS, centering=(0.5, 0.4))
        canvas.paste(fit, (x0, y0))
    return Image.alpha_composite(canvas, Image.fromarray(arr)).convert("RGB")


def compose_preview(template_path, photo_paths):
    """Show the appropriate live preview: full Landscape sheet or one portrait panel."""
    path = Path(template_path)
    slots = get_slots(path)
    canvas = Image.open(path).convert("RGBA")
    photos = [ImageOps.exif_transpose(Image.open(photo)).convert("RGB") for photo in photo_paths]
    if path.name.lower() == "landscape.jpg":
        live_slots = [
            (526, 74, 423, 275),
            (44, 407, 282, 184),
            (358, 408, 284, 184),
            (674, 407, 282, 184),
        ]
        for i, (x, y, w, h) in enumerate(live_slots):
            if i >= len(photos):
                break
            x0, y0 = max(x - 2, 0), max(y - 2, 0)
            x1, y1 = min(x + w + 2, canvas.width), min(y + h + 2, canvas.height)
            fit = ImageOps.fit(photos[i], (x1 - x0, y1 - y0), Image.LANCZOS,
                               centering=(0.5, 0.4))
            canvas.paste(fit, (x0, y0))
    else:
        for i, (x, y, w, h) in enumerate(slots[:PHOTOS_NEEDED]):
            if i >= len(photos):
                break
            x0, y0 = max(x - 2, 0), max(y - 2, 0)
            x1, y1 = min(x + w + 2, canvas.width), min(y + h + 2, canvas.height)
            fit = ImageOps.fit(photos[i], (x1 - x0, y1 - y0), Image.LANCZOS,
                               centering=(0.5, 0.4))
            canvas.paste(fit, (x0, y0))
    overlay = Image.open(prepare_template(path)).convert("RGBA")
    preview = Image.alpha_composite(canvas, overlay)
    if path.name.lower() == "landscape.jpg":
        left = 24
        top = 55
        right = 980
        bottom = 670
        return preview.crop((left, top, right, bottom)).convert("RGB")

    groups = [slots[i:i + PHOTOS_NEEDED] for i in range(0, len(slots), PHOTOS_NEEDED)]
    if len(groups) > 1:
        target = groups[0]
        x0, y0 = min(s[0] for s in target), min(s[1] for s in target)
        x1 = max(s[0] + s[2] for s in target)
        y1 = max(s[1] + s[3] for s in target)

        def bounds(axis, start, end, size):
            center = (start + end) / 2
            if axis == "x":
                centers = [(min(s[0] for s in group) + max(s[0] + s[2] for s in group)) / 2
                           for group in groups[1:]]
            else:
                centers = [(min(s[1] for s in group) + max(s[1] + s[3] for s in group)) / 2
                           for group in groups[1:]]
            tolerance = max(10, (end - start) * 0.1)
            before = [value for value in centers if value < center - tolerance]
            after = [value for value in centers if value > center + tolerance]
            return (int((max(before) + center) / 2) if before else 0,
                    int((min(after) + center) / 2) if after else size)

        left, right = bounds("x", x0, x1, preview.width)
        top, bottom = bounds("y", y0, y1, preview.height)
        preview = preview.crop((left, top, right, bottom))
    return preview.convert("RGB")


def batch_capacity(template_path):
    """Sessions per A4 for the selected template."""
    template_path = Path(template_path)
    if template_path.name.lower() == "landscape.jpg":
        return 4
    slot_count = len(get_slots(template_path))
    if slot_count <= 4:
        return 4
    return slot_count // PHOTOS_NEEDED


def compose_batch(template_path, sessions):
    """Compose queued sessions into a template-aware A4 batch."""
    template_path = Path(template_path)
    slots = get_slots(template_path)
    capacity = batch_capacity(template_path)
    sessions = sessions[:capacity]

    if template_path.name.lower() == "landscape.jpg":
        panel_slots = [
            [(526, 74, 423, 275), (44, 407, 282, 184), (358, 408, 284, 184), (674, 407, 282, 184)],
            [(1531, 74, 422, 275), (1049, 407, 281, 184), (1364, 407, 282, 184), (1679, 407, 282, 184)],
            [(526, 786, 422, 276), (44, 1120, 282, 183), (359, 1120, 283, 183), (674, 1121, 282, 183)],
            [(1531, 786, 422, 276), (1049, 1120, 283, 183), (1363, 1120, 282, 183), (1679, 1121, 282, 183)],
        ]
        page = Image.open(template_path).convert("RGBA")
        tpl = Image.open(prepare_template(template_path)).convert("RGBA")
        for photos, panel in zip(sessions, panel_slots):
            photo_files = [ImageOps.exif_transpose(Image.open(photo)).convert("RGB") for photo in photos]
            for photo_index, (x, y, w, h) in enumerate(panel):
                x0, y0 = max(x - 2, 0), max(y - 2, 0)
                x1, y1 = min(x + w + 2, page.width), min(y + h + 2, page.height)
                fit = ImageOps.fit(photo_files[photo_index], (x1 - x0, y1 - y0), Image.LANCZOS,
                                   centering=(0.5, 0.4))
                page.paste(fit, (x0, y0))
        return Image.alpha_composite(page, tpl).convert("RGB")

    if len(slots) == PHOTOS_NEEDED:
        copies = []
        for photos in sessions:
            copies.append(compose(template_path, photos))
        while len(copies) < capacity:
            copies.append(Image.open(template_path).convert("RGB"))
        pw, ph = PAGES["4x6"]
        pw, ph = ph, pw
        page = Image.new("RGB", (pw, ph), "white")
        cols, rows = 2, 2
        cell_w, cell_h = pw // cols, ph // rows
        for i, copy in enumerate(copies[:4]):
            copy.thumbnail((cell_w, cell_h), Image.LANCZOS)
            x = i % cols * cell_w + (cell_w - copy.width) // 2
            y = i // cols * cell_h + (cell_h - copy.height) // 2
            page.paste(copy, (x, y))
        return page

    original = Image.open(template_path).convert("RGBA")
    tpl = Image.open(prepare_template(template_path)).convert("RGBA")
    canvas = original.copy()
    for session_index, photos in enumerate(sessions):
        photo_files = [ImageOps.exif_transpose(Image.open(photo)).convert("RGB") for photo in photos]
        panel_slots = slots[session_index * PHOTOS_NEEDED:(session_index + 1) * PHOTOS_NEEDED]
        for photo_index, (x, y, w, h) in enumerate(panel_slots):
            x0, y0 = max(x - 2, 0), max(y - 2, 0)
            x1, y1 = min(x + w + 2, original.width), min(y + h + 2, original.height)
            fit = ImageOps.fit(photo_files[photo_index], (x1 - x0, y1 - y0), Image.LANCZOS,
                               centering=(0.5, 0.4))
            canvas.paste(fit, (x0, y0))
    return Image.alpha_composite(canvas, tpl).convert("RGB")


def make_page(strip, paper, full_sheet=False):
    """Lay out strip copies on the paper at 100% (scaled only if too large to fit)."""
    key, copies = {"4x6_one_strip": ("4x6", 1), "4x6_two_strips": ("4x6", 2)}.get(paper, ("a4", 0))
    pw, ph = PAGES[key]
    if strip.width > strip.height:
        pw, ph = ph, pw
    if full_sheet:
        scale = min(pw / strip.width, ph / strip.height)
        fitted = strip.resize((int(strip.width * scale), int(strip.height * scale)), Image.LANCZOS)
        page = Image.new("RGB", (pw, ph), "white")
        page.paste(fitted, ((pw - fitted.width) // 2, (ph - fitted.height) // 2))
        return page
    if abs(strip.width / strip.height - pw / ph) < 0.02 * pw / ph:   # template is already a full sheet
        return strip if strip.size == (pw, ph) else strip.resize((pw, ph), Image.LANCZOS)
    if copies == 0:  # A4: as many as fit side by side
        copies = max(1, pw // strip.width)
    scale = min(1.0, ph / strip.height, pw / (strip.width * copies))
    s = strip.resize((int(strip.width * scale), int(strip.height * scale)), Image.LANCZOS) if scale < 1 else strip
    page = Image.new("RGB", (pw, ph), "white")
    x0 = (pw - s.width * copies) // 2
    for i in range(copies):
        page.paste(s, (x0 + i * s.width, (ph - s.height) // 2))
    return page


# ---------- printing ----------
def list_printers():
    try:
        import win32print
        return [printer[2] for printer in win32print.EnumPrinters(2 | 4)]
    except Exception:
        return []


def print_image(img, printer=""):
    import win32print, win32ui
    from PIL import ImageWin

    printer = printer or win32print.GetDefaultPrinter()
    dc = win32ui.CreateDC()
    dc.CreatePrinterDC(printer)
    pw = dc.GetDeviceCaps(110)
    ph = dc.GetDeviceCaps(111)
    ox = dc.GetDeviceCaps(112)
    oy = dc.GetDeviceCaps(113)

    if (img.width > img.height) != (pw > ph):
        img = img.rotate(90, expand=True)

    dc.StartDoc("Photobooth print job")
    dc.StartPage()
    ImageWin.Dib(img).draw(dc.GetHandleOutput(), (-ox, -oy, pw - ox, ph - oy))
    dc.EndPage()
    dc.EndDoc()
    dc.DeleteDC()


# ---------- folder watching ----------
def wait_until_ready(path, timeout=30):
    """Wait until the file stops growing and opens as a valid image."""
    last, t0 = -1, time.time()
    while time.time() - t0 < timeout:
        try:
            size = os.path.getsize(path)
            if size > 0 and size == last:
                with Image.open(path) as im:
                    im.load()
                return True
            last = size
        except Exception:
            pass
        time.sleep(0.7)
    return False


def start_watcher(folder, q):
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer
    seen = set()

    class H(FileSystemEventHandler):
        def _go(self, p):
            if p.lower().endswith((".jpg", ".jpeg")) and p not in seen:
                seen.add(p)
                threading.Thread(target=lambda: wait_until_ready(p) and q.put(p), daemon=True).start()
        def on_created(self, e):
            if not e.is_directory: self._go(e.src_path)
        def on_moved(self, e):
            if not e.is_directory: self._go(e.dest_path)

    obs = Observer()
    obs.schedule(H(), folder, recursive=True)
    obs.start()
    return obs


# ---------- GUI ----------
def main():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox, simpledialog
    from PIL import ImageTk

    cfg = load_config()
    q = queue.Queue()
    st = {"photos": [], "last_photos": [], "observer": None, "preview_image": None,
          "batches": {}, "batch_file": APP_DIR / "batches.json",
          "printing": set(), "print_errors": set(), "printed_pages": set()}

    if st["batch_file"].exists():
        try:
            st["batches"] = json.loads(st["batch_file"].read_text())
        except Exception:
            st["batches"] = {}

    def save_batches():
        st["batch_file"].write_text(json.dumps(st["batches"], indent=2))

    root = tk.Tk()
    root.title("Photobooth")
    root.geometry("1060x760")
    root.minsize(820, 620)

    status = tk.StringVar()
    tpl_var = tk.StringVar()

    def templates():
        d = Path(cfg["templates_folder"])
        return sorted(p.name for p in d.iterdir()
                      if p.is_file() and p.suffix.lower() in (".png", ".jpg", ".jpeg")) if d.exists() else []

    def refresh_status(msg=None):
        status.set(msg or f"{len(st['photos'])} of {PHOTOS_NEEDED} photos received")

    def restart_watcher():
        if st["observer"]:
            st["observer"].stop()
            st["observer"] = None
        Path(cfg["watch_folder"]).mkdir(parents=True, exist_ok=True)
        st["observer"] = start_watcher(cfg["watch_folder"], q)

    header = ttk.Frame(root, padding=(14, 12))
    header.pack(fill="x")
    ttk.Label(header, text="Template:").pack(side="left", padx=(0, 8))
    combo = ttk.Combobox(header, textvariable=tpl_var, state="readonly", width=36)
    combo.pack(side="left")
    ttk.Label(header, textvariable=status, font=("Segoe UI", 14, "bold")).pack(side="right", padx=8)

    body = ttk.Frame(root, padding=(14, 0, 14, 14))
    body.pack(fill="both", expand=True)
    preview_widget = ttk.Label(body, anchor="center", text="Select a template to preview")
    preview_widget.pack(side="left", fill="both", expand=True, padx=(0, 16))
    controls = ttk.Frame(body, width=250)
    controls.pack(side="right", fill="y")
    controls.pack_propagate(False)

    def update_preview(_event=None):
        name = tpl_var.get()
        if not name:
            preview_widget.configure(image="", text="Add a template to the templates folder")
            st["preview_image"] = None
            return
        try:
            photos = st["photos"] or st["last_photos"]
            preview = compose_preview(Path(cfg["templates_folder"]) / name, photos)
            max_width = max(preview_widget.winfo_width() - 24, 320)
            max_height = max(preview_widget.winfo_height() - 24, 420)
            preview.thumbnail((max_width, max_height), Image.LANCZOS)
            st["preview_image"] = ImageTk.PhotoImage(preview)
            preview_widget.configure(image=st["preview_image"], text="")
        except Exception as e:
            st["preview_image"] = None
            preview_widget.configure(image="", text=f"Preview unavailable:\n{e}")

    def reload_templates():
        combo["values"] = templates()
        if combo["values"] and tpl_var.get() not in combo["values"]:
            tpl_var.set(combo["values"][0])
        update_preview()

    combo.bind("<<ComboboxSelected>>", update_preview)
    preview_widget.bind("<Configure>", update_preview)

    def save_session():
        name = tpl_var.get()
        if not name:
            refresh_status("Pick a template first!"); return
        if len(st["photos"]) != PHOTOS_NEEDED:
            refresh_status(f"Need 4 photos before saving ({len(st['photos'])} received)"); return

        copy_count = simpledialog.askinteger("Session copies", "How many copies of this 4-photo session?",
                                             parent=root, initialvalue=1, minvalue=1, maxvalue=8)
        if copy_count is None:
            refresh_status("Save cancelled (0 of 4)"); return

        template = Path(cfg["templates_folder"]) / name
        sessions = st["batches"].setdefault(name, [])
        session_photos = [str(Path(photo)) for photo in st["photos"][:PHOTOS_NEEDED]]
        sessions.extend([list(session_photos) for _ in range(copy_count)])
        save_batches()
        st["last_photos"] = st["photos"][:PHOTOS_NEEDED]
        st["photos"] = []
        capacity = batch_capacity(template)

        if len(sessions) >= capacity:
            full_sessions = sessions[:capacity]
            overflow = sessions[capacity:]
            st["batches"][name] = full_sessions
            print_batch(name)
            if overflow:
                st["batches"][name] = overflow
                save_batches()
                refresh_status(f"Overflow saved: {len(overflow)} copies for next {name} A4")
        else:
            refresh_status(f"{copy_count} copies saved: {len(sessions)} of {capacity} panels used (0 of 4)")
            update_preview()

    def print_batch(name):
        template = Path(cfg["templates_folder"]) / name
        sessions = st["batches"].get(name, [])
        if not sessions:
            refresh_status("No saved sessions for this template"); return
        try:
            page = compose_batch(template, sessions)
            out = Path(cfg["output_folder"]); out.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            page_path = out / f"page_{stamp}.jpg"
            page.save(page_path, quality=95, dpi=(cfg["dpi"],) * 2)
        except Exception as e:
            messagebox.showerror("Save failed", str(e)); return
        overflow = sessions[batch_capacity(template):]
        if overflow:
            st["batches"][name] = overflow
        else:
            st["batches"].pop(name, None)
        save_batches()
        st["photos"] = []
        refresh_status(f"Saved batch: {page_path.name} (0 of 4)")
        update_preview()

    def print_now():
        name = tpl_var.get()
        if not name:
            refresh_status("Pick a template first!"); return
        sessions = st["batches"].get(name, [])
        if not sessions:
            refresh_status("No saved sessions to print"); return
        print_batch(name)

    def process_print_jobs():
        if not cfg["auto_print"]:
            return
        if not st["printing"]:
            out = Path(cfg["output_folder"])
            printed = Path(cfg["printed_folder"])
            out.mkdir(parents=True, exist_ok=True)
            printed.mkdir(parents=True, exist_ok=True)
            for page_path in sorted(out.glob("page_*.jpg")):
                key = str(page_path)
                if key in st["printing"] or key in st["printed_pages"]:
                    continue

                def print_and_archive(path=page_path, key=key):
                    st["printing"].add(key)
                    try:
                        with Image.open(path) as image:
                            print_image(image.copy(), cfg["printer"])
                        destination = printed / path.name
                        if destination.exists():
                            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                            destination = printed / f"{path.stem}_{stamp}{path.suffix}"
                        path.replace(destination)
                        st["printed_pages"].add(key)
                        st["print_errors"].discard(key)
                    except Exception as error:
                        st["print_errors"].add(key)
                        root.after(0, lambda: messagebox.showerror("Print failed",
                                                                  f"Could not print {path.name}:\n{error}"))
                    finally:
                        st["printing"].discard(key)

                threading.Thread(target=print_and_archive, daemon=True).start()
                break

    def poll():
        try:
            while True:
                photo = q.get_nowait()
                if not st["photos"]:
                    st["last_photos"] = []
                st["photos"].append(photo)
                refresh_status()
                update_preview()
                if len(st["photos"]) >= PHOTOS_NEEDED:
                    refresh_status("4 of 4 received - press Save session")
        except queue.Empty:
            pass
        process_print_jobs()
        root.after(300, poll)

    def open_folder(folder_key):
        folder = Path(cfg[folder_key])
        folder.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(folder))
        except (AttributeError, OSError) as e:
            messagebox.showerror("Could not open folder", str(e))

    def open_output_folder():
        open_folder("output_folder")

    def open_printed_folder():
        open_folder("printed_folder")

    def reset():
        st["photos"] = []; st["last_photos"] = []
        refresh_status("Session reset (0 of 4)"); update_preview()

    def undo_last():
        if st["photos"]: st["photos"].pop()
        refresh_status(); update_preview()

    def settings():
        w = tk.Toplevel(root); w.title("Settings"); w.grab_set()
        vars_ = {k: tk.StringVar(value=cfg[k]) for k in ("watch_folder", "templates_folder", "output_folder", "printed_folder", "printer", "paper")}
        for i, (k, label) in enumerate([("watch_folder", "Watched (photo) folder"), ("templates_folder", "Templates folder"),
                                        ("output_folder", "forPrint folder"), ("printed_folder", "Printed folder")]):
            ttk.Label(w, text=label).grid(row=i, column=0, sticky="w", padx=6, pady=4)
            ttk.Entry(w, textvariable=vars_[k], width=42).grid(row=i, column=1)
            ttk.Button(w, text="Browse", command=lambda k=k: vars_[k].set(filedialog.askdirectory() or vars_[k].get())
                       ).grid(row=i, column=2, padx=4)
        ttk.Label(w, text="Printer").grid(row=4, column=0, sticky="w", padx=6)
        ttk.Combobox(w, textvariable=vars_["printer"], values=[""] + list_printers(), width=40).grid(row=4, column=1)
        ttk.Label(w, text="Paper layout").grid(row=5, column=0, sticky="w", padx=6, pady=4)
        ttk.Combobox(w, textvariable=vars_["paper"], values=["4x6_one_strip", "4x6_two_strips", "a4"],
                 state="readonly", width=40).grid(row=5, column=1)

        auto_print_var = tk.BooleanVar(value=cfg["auto_print"])
        ttk.Checkbutton(w, text="Print automatically", variable=auto_print_var).grid(row=6, column=1, sticky="w", pady=4)

        def ok():
            for k, v in vars_.items(): cfg[k] = v.get()
            cfg["auto_print"] = auto_print_var.get()
            ensure_output_folders(cfg)
            save_config(cfg); restart_watcher(); reload_templates(); w.destroy()
        ttk.Button(w, text="Save", command=ok).grid(row=7, column=1, pady=10)

    for text, cmd in [("Save session", save_session), ("Print now", print_now),
                      ("Open forPrint folder", open_output_folder), ("Open Printed folder", open_printed_folder),
                      ("Remove last photo (retake)", undo_last), ("Reset session", reset), ("Settings", settings)]:
        ttk.Button(controls, text=text, command=cmd, width=28).pack(pady=6)

    reload_templates(); refresh_status(); update_preview(); restart_watcher(); poll()
    root.protocol("WM_DELETE_WINDOW", lambda: (st["observer"] and st["observer"].stop(), root.destroy()))
    root.mainloop()


if __name__ == "__main__":
    main()
