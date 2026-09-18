# Flyer Builder — A5 Window Films / Car Wrapping

ExtendScript (`.jsx`) automation that builds a **complete, print‑ready two‑sided A5
portrait flyer** in Adobe InDesign.

| | |
|---|---|
| Page 1 | **FENSTERFOLIERUNGEN** — bright, architectural, trustworthy |
| Page 2 | **AUTOFOLIERUNGEN** — dark, dynamic, premium, automotive |
| Format | A5 portrait, 148 × 210 mm |
| Bleed | 3 mm all sides |
| Safe margin | 10 mm |
| Pages | 2, facing pages **off** |
| Colour | CMYK process workflow |
| Layers | `01_BACKGROUND` · `02_IMAGES` · `03_GRAPHICS` · `04_TEXT` · `05_BRANDING` |
| Paragraph styles | `Headline` · `Subheadline` · `Benefit` · `CTA` · `Contact` · `SmallPrint` |

The script **creates a new document**. It never edits, saves over, or closes an
already‑open document. Assets resolve **next to the `.jsx`** (no Desktop path).
A missing **required hero** (`Fensterfolierung2.png` / `Autofolierung2.png`)
draws a placeholder and makes validation **FAIL**; a missing **optional**
`logo.png` / `qr.png` draws a placeholder and is a **`[WARN]` only**. Missing
fonts fall back down the priority list (last entry is a Windows‑safe guarantee).
Overset text is auto‑healed (type shrunk to a per‑style floor) before validation.

---

## 1. Run the script in InDesign

**Option A — Scripts panel (recommended)**

1. `Window > Utilities > Scripts`
2. Right‑click the **User** folder → **Reveal in Explorer**
3. Copy `flyer_builder.jsx` into that folder
4. Back in InDesign, double‑click `flyer_builder.jsx` in the Scripts panel

**Option B — one‑off**

`File > Scripts > Other Script…` → select `flyer_builder.jsx`

**Option C — VS Code / ExtendScript**

Run with target application **Adobe InDesign** (the file starts with
`#target "indesign"`).

When it finishes, a **validation report** dialog appears (also written to the
ExtendScript console).

---

## 2. Change contact information

Open `flyer_builder.jsx`, edit the `CONFIG.CONTACT` block near the top:

```jsx
CONTACT: {
    companyName: "MUSTERFIRMA FOLIENTECHNIK",
    phone:       "+49 000 00 00 000",
    email:       "info@musterfirma-folien.de",
    website:     "www.musterfirma-folien.de",
    instagram:   "@musterfirma.folien"
},
```

Used identically in the branding block on **both** pages.

---

## 3. Assets — put them next to the script

The script resolves every asset **relative to the folder that contains
`flyer_builder.jsx`** (`<scriptFolder>/<file name>`). There is **no Desktop
dependency**. Drop these files beside the `.jsx`:

| File | Role | Severity |
|---|---|---|
| `Fensterfolierung2.png` | **Page 1 hero** (placed) | **required** |
| `Autofolierung2.png` | **Page 2 hero** (placed) | **required** |
| `Fensterfolierung.png` | design reference only — **not placed** | ignored in output |
| `Autofolierung.png` | design reference only — **not placed** | ignored in output |
| `logo.png` | branding logo | **optional** |
| `qr.png` | branding QR code | **optional** |

```jsx
WINDOW_IMAGE_FILE: "Fensterfolierung2.png",   // Page 1 hero  (required)
CAR_IMAGE_FILE:    "Autofolierung2.png",       // Page 2 hero  (required)
WINDOW_REF_FILE:   "Fensterfolierung.png",     // reference only
CAR_REF_FILE:      "Autofolierung.png",        // reference only
LOGO_FILE:         "logo.png",                 // optional
QR_FILE:           "qr.png",                   // optional

// Optional hard overrides — absolute path ("~" allowed); "" = use script folder
WINDOW_IMAGE_PATH: "",
CAR_IMAGE_PATH:    "",
LOGO_PATH:         "",
QR_PATH:           "",
```

* **Required hero present** → placed, scaled *fill proportional*, centred, not distorted.
* **Required hero missing** → placeholder drawn **and validation FAILS**
  (listed under *OFFENE PFLICHT-PLATZHALTER*).
* **Optional logo/QR missing** → placeholder drawn, reported as `[WARN]` only —
  the overall flyer does **not** fail because of it.
* An absolute `*_PATH` override wins over the script-folder lookup for that one asset.

`Fensterfolierung.png` / `Autofolierung.png` are the visual design targets for
each page (bright/architectural vs. dark/automotive). They are never placed in
the print output.

---

## 4. Replace images manually (after the script ran)

1. `Window > Links`
2. Select the link (or the empty placeholder frame → click it, then
   `File > Place` into it)
3. **Relink** button → choose the new file
4. With the frame selected: `Object > Fitting > Fill Frame Proportionally`,
   then `Object > Fitting > Center Content`

Frame positions/sizes stay fixed — only the content swaps.

---

## 5. Change colours

`CONFIG.COLORS` — every value is **CMYK `[C, M, Y, K]`, 0–100**:

```jsx
COLORS: {
    PRIMARY:          [92, 60,  8, 12],   // deep architectural blue
    SECONDARY:        [22, 14, 12, 34],   // cool neutral grey
    ACCENT:           [ 0, 66, 96,  0],   // warm signal orange (CTA + markers)
    BACKGROUND_LIGHT: [ 3,  1,  0,  0],   // page 1 background
    BACKGROUND_DARK:  [72, 62, 58, 72],   // page 2 background
    INK:              [ 0,  0,  0, 92],   // text on light
    LIGHT_TEXT:       [ 0,  0,  0,  6],   // text on dark
    GRAY60:           [ 0,  0,  0, 55],   // small print
    PLACEHOLDER:      [ 0,  0,  0, 12]    // empty image frame
},
```

Re‑run the script. Swatches are created as `FLYER_Primary`, `FLYER_Accent`, …
To retune inside an existing document instead: `Window > Color > Swatches`,
double‑click a `FLYER_*` swatch, keep type **Process / CMYK**.

---

## 6. Change fonts

`CONFIG.HEADLINE_FONT` and `CONFIG.BODY_FONT` are **priority lists**. The first
installed family wins; the last entry is the guaranteed fallback.

```jsx
HEADLINE_FONT: ["Montserrat", "Segoe UI", "Tahoma", "Arial", "Verdana"],
BODY_FONT:     ["Segoe UI", "Arial", "Tahoma", "Verdana"],

HEADLINE_WEIGHTS: ["Bold", "SemiBold", "Semibold", "Black", "Heavy"],
BODY_WEIGHTS:     ["Regular", "Semilight", "Light"],
```

* No rare font is a hard dependency. `Segoe UI` and `Arial` ship with Windows.
* `Montserrat` is free on Adobe Fonts / Google Fonts — activate it for the
  intended bold sans‑serif look, otherwise it silently falls back.
* Want a different look? Put your family name **first** in the list and make sure
  a matching weight name is in the `*_WEIGHTS` list.

The report prints which families were actually resolved.

---

## 7. Export the flyer for professional print

**Automatic:** set `CONFIG.EXPORT_PDF: true` (and check `PDF_PRESET`,
default `[PDF/X-4:2008]`). The script exports next to the `.indd` with crop marks,
registration marks, colour bars and **document bleed**. If the preset is not
installed the export is skipped, the `.indd` stays intact, and the report says so.

`CONFIG.OUTPUT_FOLDER: ""` writes the `.indd` (and PDF) **next to the script**.
Set an absolute path or `"~/Desktop"` to change that.

`[OK] PDF/X-4 Export-Bereitschaft` is printed **only when every mandatory
criterion passed** (both heroes placed, no overset, no broken required links,
document saved). Missing logo/QR do not block it.

**Manual:**

1. `File > Export…` → format **Adobe PDF (Print)**
2. Preset: **`[PDF/X-4:2008]`** (or your printer's preset)
3. **Marks and Bleeds** tab:
   * Crop Marks ✔, Registration Marks ✔, Colour Bars ✔, Page Information ✔
   * Bleed: **Use Document Bleed Settings** ✔ (3 mm)
4. **Output** tab: Colour Conversion → *Convert to Destination*,
   Destination → your print profile (e.g. *ISO Coated v2* / *PSO Coated*)
5. Export → send the PDF to the print shop.

---

## 8. Final validation report

The report separates **mandatory** criteria (a single `[!!]` here makes the
overall status `FAIL`) from **optional** branding (`[WARN]` only, never a fail).

**Mandatory — all must be `[OK]` for `GESAMT-STATUS: PASS`:**

| Check | Where |
|---|---|
| script completed without exception | report appeared at all |
| **A5** 148 × 210 mm | `File > Document Setup` |
| **3 mm bleed** on all four sides | `Document Setup > Bleed and Slug` |
| **2 pages**, facing pages **OFF** | Pages panel |
| **`Fensterfolierung2.png` placed** (Page 1 hero) | Links panel |
| **`Autofolierung2.png` placed** (Page 2 hero) | Links panel |
| **No overset text** — no red `+` on any frame | Preflight / report |
| **No broken required links** | `Window > Links` — no ⚠ |
| all `FLYER_*` swatches Process **CMYK** | Swatches panel |
| **document saved** | report line `PFLICHT: Dokument gespeichert` |

**Optional — `[WARN]` does not fail the flyer:**

| Check |
|---|
| `logo.png` supplied |
| `qr.png` supplied |
| real contact data in `CONFIG.CONTACT` (not `MUSTERFIRMA…`) |

**Informational:** effective image resolution ≥ 220 ppi (aim for ≥ 300),
PDF/X-4 export readiness (gated on all mandatory criteria passing).

Overset text is also auto-healed before validation: any frame that would
overflow with a substitute font is shrunk in 0.5 pt steps down to a floor
(body 7.5 pt, small print 6.5 pt, headline 20 pt) — hierarchy is preserved.

---

## 9. Common runtime problems

| Symptom | Cause / fix |
|---|---|
| `ReferenceError: <Enum> is undefined` | An InDesign enumeration name was mistyped. Every enum the script sets is now wrapped in `try/catch`, so a version mismatch degrades gracefully instead of aborting. Report the exact name if it recurs. |
| `flyer_builder: Dieses Script muss in Adobe InDesign laufen.` | Ran under the wrong target app. Use InDesign (Scripts panel) or set the ExtendScript target to Adobe InDesign. |
| `OFFENE PFLICHT-PLATZHALTER` | A **required hero** (`Fensterfolierung2.png` / `Autofolierung2.png`) was not found next to the script. Put the file there (or set the matching absolute `*_IMAGE_PATH`) and re‑run. This **fails** validation. |
| `OFFENE OPTIONALE PLATZHALTER` | `logo.png` / `qr.png` not supplied. Placeholder drawn, `[WARN]` only — the flyer still passes. Add the files later and re‑run, or relink manually (§4). |
| Assets not found even though they exist | The script folder could not be resolved (rare — e.g. run from a raw `eval`). Set absolute `WINDOW_IMAGE_PATH` / `CAR_IMAGE_PATH` etc. |
| `PDF-Preset nicht gefunden` | `CONFIG.PDF_PRESET` is not installed. Export is skipped, the `.indd` is left intact — export manually (§7). |
| `Speichern fehlgeschlagen` | The output folder is not writable (`CONFIG.OUTPUT_FOLDER`, or the script folder when it is `""`). Point it at a writable folder. Blocks `PASS`. |
| `UEBERSATZ NICHT behoben` | A frame is still overset after auto‑heal hit its type floor. Widen/heighten that frame in the `.jsx`, or shorten the copy. Blocks `PASS`. |
| Headline uses a fallback font | `Montserrat` (or your first choice) is not activated. Activate it, or accept the `Segoe UI` fallback. The report prints the family actually used. |
| Re‑running accumulates nothing | Safe. Swatches and paragraph styles are upserted by name; each run builds a fresh document. |
