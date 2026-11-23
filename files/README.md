# Photobooth strip app – setup

## 1. One-time setup (Windows)
1. Install Python 3.11+ from python.org (tick "Add Python to PATH").
2. Double-click `build_exe.bat`. It creates `dist\Photobooth.exe`.
3. Copy `Photobooth.exe` to a folder, create a `templates` folder beside it and put your Canva PNG or JPG templates inside.
   Finished print pages are saved in a `forPrint` folder beside the app by default.
   (Or skip the exe and run `python photobooth.py`.)

## 2. Canva templates
Export each design as it is: Share → Download → PNG, full size. Keep the placeholder pictures in the frames. PNG is preferred for image quality; JPG is supported too.
- Put the PNG or JPG files in the `templates` folder. On first use the app makes a transparent PNG working copy in `templates\prepared` and saves the slot positions in `templates\slots.json` (editable).
- A template that is a full 4x6 sheet with two strips gets the same 4 photos in both strips.
- If you re-export a template, the app re-detects it automatically.
- If a design's windows are not detected, paint each photo area in solid magenta (#FF00FF) under the frame and re-export.
- Shoot the Nikon in **landscape**; the windows are landscape (about 3:2).

## 3. digiCamControl (Nikon tethering)
1. Install digiCamControl, connect the Nikon by USB (camera ON; set to Manual/PC mode).
2. Camera: set image quality to **JPEG Fine** (or RAW+JPEG – the app only reads JPEGs).
3. In digiCamControl: Settings → Session → set the save folder (default `Pictures\digiCamControl\Session1`). Enable "Auto transfer" so shots go to the PC.
4. In the app: Settings → Watched folder = the same folder.
5. Press the physical shutter. Each transferred JPEG appears in the preview and advances the photo count.

## 4. Automatic printing
Photobooth is printer-brand independent. It uses the Windows default printer unless a specific printer is selected in Settings → Printer.

Any new `page_*.jpg` in `forPrint` is printed automatically when **Print automatically** is enabled. A successful job is moved to `Printed`; a failed job remains in `forPrint` and shows an error. Windows printer preferences control paper size, media, quality, borderless mode and scaling.

For 4×6 or A4 testing, set the Windows printer preferences for the test Epson or later Brother first. No Epson/Brother name is stored in the application.
2. In Photobooth Settings, choose the paper layout:
   - `4x6_two_strips`: two 2x6 strips side by side on one 4x6 sheet (cut in half)
   - `4x6_one_strip`: one strip centred on 4x6
   - `a4`: copies tiled across A4
3. Open the saved `page_*.jpg`, choose **Print**, and select the Brother printer. Print a test and check the strip isn't shrunk or cropped.

## 5. Each session and A4 batching
Pick a template → press the camera shutter 4 times → each transferred photo fills the next preview slot.

Click **Save session** only after the four photos are complete. The app asks how many copies of the identical 4-photo session the guest wants. Those copies fill separate template panel positions. FRAME 1, FRAME 2, and FRAME 3 hold 8 session-panel positions per A4 sheet. The Landscape template holds 4 session-panel positions per A4 sheet.

Use **Print now** to save the partial or complete batch as a print-ready JPG in `forPrint`. When the batch reaches the template's full session capacity, it is saved automatically. The printer remains disabled until printer integration is completed.

Later, after a successful automatic print, the JPG will move from `forPrint` to `Printed`. Failed prints will leave it in `forPrint` and show an error.

Buttons: Save session, Print now, Open forPrint folder, Open Printed folder, Remove last photo (retake), Reset session.
