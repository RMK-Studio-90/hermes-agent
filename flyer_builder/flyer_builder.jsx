/* =====================================================================
 * flyer_builder.jsx
 * ---------------------------------------------------------------------
 * Production two-sided A5 portrait advertising flyer.
 *   PAGE 1 - WINDOW FILMS  (Fensterfolierungen)  - bright / architectural
 *   PAGE 2 - CAR WRAPPING  (Autofolierungen)     - dark / premium
 *
 * Target : Adobe InDesign on Windows (ExtendScript / JSX)
 * Output : one NEW InDesign document (never touches an open document)
 *
 * HOW TO RUN
 *   1. Copy this file into the InDesign "Scripts Panel" folder
 *      (Window > Utilities > Scripts  ->  right-click "User" > Reveal in Explorer)
 *   2. In InDesign: Window > Utilities > Scripts  ->  double-click flyer_builder.jsx
 *   OR: File > Scripts > Other Script... and pick this file.
 *
 * EVERYTHING YOU NORMALLY CHANGE IS IN THE  CONFIG  BLOCK BELOW.
 * =================================================================== */

#target "indesign"

/* =====================================================================
 * 1. CONFIG  -  edit these values, nothing else.
 * =================================================================== */
var CONFIG = {

    /* ---- Fonts -------------------------------------------------------
     * List = priority order. First installed family wins.
     * Keep at least one Windows-safe fallback (Segoe UI / Arial).
     * No rare font is hard-coded as a hard dependency.               */
    HEADLINE_FONT: ["Montserrat", "Segoe UI", "Tahoma", "Arial", "Verdana"],
    BODY_FONT:     ["Segoe UI", "Arial", "Tahoma", "Verdana"],

    /* Weight names the script will try for bold text, in order.       */
    HEADLINE_WEIGHTS: ["Bold", "SemiBold", "Semibold", "Black", "Heavy"],
    BODY_WEIGHTS:     ["Regular", "Semilight", "Light"],

    /* ---- Design system colours  (CMYK: [C, M, Y, K], 0-100) --------*/
    COLORS: {
        PRIMARY:          [92, 60,  8, 12],   /* deep architectural blue  */
        SECONDARY:        [22, 14, 12, 34],   /* cool neutral grey        */
        ACCENT:           [ 0, 66, 96,  0],   /* warm signal orange (CTA) */
        BACKGROUND_LIGHT: [ 3,  1,  0,  0],   /* page 1 paper background  */
        BACKGROUND_DARK:  [72, 62, 58, 72],   /* page 2 soft rich black   */
        INK:              [ 0,  0,  0, 92],   /* body text on light       */
        LIGHT_TEXT:       [ 0,  0,  0,  6],   /* body text on dark        */
        GRAY60:           [ 0,  0,  0, 55],   /* small print              */
        PLACEHOLDER:      [ 0,  0,  0, 12]    /* empty image frame fill   */
    },

    /* ---- Contact / branding information ---------------------------- */
    CONTACT: {
        companyName: "MUSTERFIRMA FOLIENTECHNIK",
        phone:       "+49 000 00 00 000",
        email:       "info@musterfirma-folien.de",
        website:     "www.musterfirma-folien.de",
        instagram:   "@musterfirma.folien"
    },

    /* ---- Assets  (live NEXT TO this .jsx - no Desktop dependency) ------
     * The script resolves every asset relative to the folder that
     * contains flyer_builder.jsx:   <scriptFolder>/<file name>.
     *
     *   Fensterfolierung2.png  = Page 1 hero        (REQUIRED)
     *   Autofolierung2.png     = Page 2 hero        (REQUIRED)
     *   Fensterfolierung.png   = design reference   (NOT placed)
     *   Autofolierung.png      = design reference   (NOT placed)
     *   logo.png               = branding logo      (OPTIONAL)
     *   qr.png                 = branding QR code   (OPTIONAL)
     *
     * A missing REQUIRED hero -> validation FAILS.
     * A missing OPTIONAL logo/QR -> validation WARNS only, build continues
     * with a labelled placeholder frame.                               */
    WINDOW_IMAGE_FILE: "Fensterfolierung2.png",   /* Page 1 hero  (required) */
    CAR_IMAGE_FILE:    "Autofolierung2.png",       /* Page 2 hero  (required) */
    WINDOW_REF_FILE:   "Fensterfolierung.png",     /* reference only - not placed */
    CAR_REF_FILE:      "Autofolierung.png",        /* reference only - not placed */
    LOGO_FILE:         "logo.png",                 /* optional */
    QR_FILE:           "qr.png",                   /* optional */

    /* Optional hard overrides. Set an ABSOLUTE path ("~" allowed) to
     * bypass the script-folder lookup for that one asset. "" = use the
     * script folder + *_FILE name above.                               */
    WINDOW_IMAGE_PATH: "",
    CAR_IMAGE_PATH:    "",
    LOGO_PATH:         "",
    QR_PATH:           "",

    /* ---- Output --------------------------------------------------- */
    SAVE_INDD:     true,
    OUTPUT_FOLDER: "",                    /* "" = next to the script; "~/Desktop" etc. also allowed */
    INDD_NAME:     "Flyer_Folientechnik_A5.indd",

    EXPORT_PDF:    false,                 /* set true for automatic print PDF */
    PDF_NAME:      "Flyer_Folientechnik_A5.pdf",
    PDF_PRESET:    "[PDF/X-4:2008]"       /* falls back gracefully if missing */
};

/* =====================================================================
 * 2. Document geometry (mm) - the brief's fixed spec.
 * =================================================================== */
var PAGE_W  = 148;
var PAGE_H  = 210;
var BLEED   = 3;
var MARGIN  = 10;

var BX_L = -BLEED,           BX_R = PAGE_W + BLEED;   /* -3 .. 151 */
var BX_T = -BLEED,           BX_B = PAGE_H + BLEED;   /* -3 .. 213 */
var CL   = MARGIN,           CR   = PAGE_W - MARGIN;  /* 10  .. 138 */

/* Runtime state ------------------------------------------------------ */
var doc      = null;
var SW       = {};          /* resolved swatches            */
var HF       = "";          /* resolved headline family     */
var BF       = "";          /* resolved body family         */
var LOG      = [];
var MISSING  = [];          /* legacy aggregate (all placeholders) */
var MISS_REQ = [];          /* missing REQUIRED assets (hero images) */
var MISS_OPT = [];          /* missing OPTIONAL assets (logo / QR)   */
var PLACED   = { windowHero: false, carHero: false, logo: false, qr: false };
var SAVED    = false;       /* set true once doc.save() succeeds     */
var _saved   = {};          /* saved app prefs for restore  */

/* Folder that contains this .jsx - all assets resolve against it. */
var SCRIPT_DIR = resolveScriptDir();

function resolveScriptDir() {
    var f = null;
    try {
        if (app.activeScript) {
            f = (app.activeScript instanceof File) ? app.activeScript : new File(app.activeScript);
        }
    } catch (e) { f = null; }               /* activeScript throws when run from ESTK */
    if (!f) { try { f = new File($.fileName); } catch (e2) { f = null; } }
    if (f && f.parent) return f.parent;
    return Folder("~");                      /* last resort - should not happen on a normal run */
}

/* Build a File for an asset: absolute override wins, else <scriptDir>/<name>. */
function assetFile(absOverride, fileName) {
    if (absOverride && String(absOverride).length) {
        return new File(resolvePath(String(absOverride)));
    }
    if (!fileName) return null;
    return new File(SCRIPT_DIR.fsName + "/" + fileName);
}

/* =====================================================================
 * 3. MAIN
 * =================================================================== */
function main() {

    if (!/InDesign/i.test(String(app.name))) {
        alert("flyer_builder: Dieses Script muss in Adobe InDesign laufen.");
        return;
    }

    try {
        savePrefs();

        doc = app.documents.add();               /* NEW document only */
        setupDocument();
        buildLayers();
        buildColors();
        resolveFonts();
        buildParagraphStyles();

        buildPage1();
        buildPage2();

        noteReferences();
        healOverset();           /* eliminate any residual overset text */

        finishOutput();
        report();
    }
    catch (e) {
        try { $.writeln("flyer_builder ERROR: " + e + " (line " + e.line + ")"); } catch (_) {}
        alert("flyer_builder: Fehler\n\n" + e + "\n\nZeile: " + (e.line || "?"));
    }
    finally {
        restorePrefs();
    }
}

/* =====================================================================
 * 4. Preferences
 * =================================================================== */
function savePrefs() {
    _saved.mu      = app.scriptPreferences.measurementUnit;
    _saved.ui      = app.scriptPreferences.userInteractionLevel;
    _saved.redraw  = app.scriptPreferences.enableRedraw;

    app.scriptPreferences.measurementUnit      = MeasurementUnits.MILLIMETERS;
    app.scriptPreferences.userInteractionLevel = UserInteractionLevels.NEVER_INTERACT; /* no missing-font/link dialogs */
    app.scriptPreferences.enableRedraw         = false;
}
function restorePrefs() {
    try { app.scriptPreferences.measurementUnit      = _saved.mu; }     catch (e) {}
    try { app.scriptPreferences.userInteractionLevel = _saved.ui; }     catch (e) {}
    try { app.scriptPreferences.enableRedraw         = _saved.redraw; } catch (e) {}
}

/* =====================================================================
 * 5. Document setup  (A5, no facing pages, 3 mm bleed, CMYK workflow)
 * =================================================================== */
function setupDocument() {
    var dp = doc.documentPreferences;

    dp.facingPages = false;
    dp.pageWidth   = PAGE_W;
    dp.pageHeight  = PAGE_H;
    try { dp.pageOrientation = PageOrientation.PORTRAIT; } catch (e) {}

    dp.documentBleedUniformSize        = true;
    dp.documentBleedTopOffset          = BLEED;
    dp.documentBleedBottomOffset       = BLEED;
    dp.documentBleedInsideOrLeftOffset = BLEED;
    dp.documentBleedOutsideOrRightOffset = BLEED;

    dp.pagesPerDocument = 2;
    while (doc.pages.length < 2) doc.pages.add();

    doc.viewPreferences.rulerOrigin              = RulerOrigin.PAGE_ORIGIN;
    doc.viewPreferences.horizontalMeasurementUnits = MeasurementUnits.MILLIMETERS;
    doc.viewPreferences.verticalMeasurementUnits   = MeasurementUnits.MILLIMETERS;

    /* CMYK-oriented workflow */
    try { doc.transparencyPreferences.blendingSpace = BlendingSpace.CMYK; } catch (e) {}

    /* Safe margins on every page (1 column) */
    for (var i = 0; i < doc.pages.length; i++) {
        var mp = doc.pages[i].marginPreferences;
        try {
            mp.top = MARGIN; mp.bottom = MARGIN; mp.left = MARGIN; mp.right = MARGIN;
            mp.columnCount = 1; mp.columnGutter = 0;
        } catch (e) {}
    }

    LOG.push("Dokument: A5 " + PAGE_W + "x" + PAGE_H + " mm, 2 Seiten, Bleed " + BLEED + " mm, Facing Pages AUS.");
}

/* =====================================================================
 * 6. Layers  (bottom -> top: 01_BACKGROUND .. 05_BRANDING)
 * =================================================================== */
var LAYER_NAMES = ["01_BACKGROUND", "02_IMAGES", "03_GRAPHICS", "04_TEXT", "05_BRANDING"];

function buildLayers() {
    var originalName = doc.layers[0].name;   /* default layer, locale-dependent name */

    /* add in order; each new layer lands on top -> final stack is
       05_BRANDING (top) ... 01_BACKGROUND (bottom above default)      */
    for (var i = 0; i < LAYER_NAMES.length; i++) {
        var ly = doc.layers.itemByName(LAYER_NAMES[i]);
        if (!ly.isValid) doc.layers.add({ name: LAYER_NAMES[i] });
    }

    /* remove the leftover default layer if it is not one of ours */
    var keep = false;
    for (var j = 0; j < LAYER_NAMES.length; j++) if (LAYER_NAMES[j] === originalName) keep = true;
    if (!keep) {
        var orig = doc.layers.itemByName(originalName);
        if (orig.isValid && doc.layers.length > LAYER_NAMES.length) {
            try { orig.remove(); } catch (e) {}
        }
    }
    LOG.push("Ebenen: " + LAYER_NAMES.join(", "));
}
function L(name) { return doc.layers.itemByName(name); }

/* =====================================================================
 * 7. Colours  (all CMYK process swatches)
 * =================================================================== */
function buildColors() {
    var c = CONFIG.COLORS;
    SW.PRIMARY     = upsertColor("FLYER_Primary",     c.PRIMARY);
    SW.SECONDARY   = upsertColor("FLYER_Secondary",   c.SECONDARY);
    SW.ACCENT      = upsertColor("FLYER_Accent",      c.ACCENT);
    SW.BG_LIGHT    = upsertColor("FLYER_BG_Light",    c.BACKGROUND_LIGHT);
    SW.BG_DARK     = upsertColor("FLYER_BG_Dark",     c.BACKGROUND_DARK);
    SW.INK         = upsertColor("FLYER_Ink",         c.INK);
    SW.LIGHT_TEXT  = upsertColor("FLYER_LightText",   c.LIGHT_TEXT);
    SW.GRAY60      = upsertColor("FLYER_Gray60",      c.GRAY60);
    SW.PLACEHOLDER = upsertColor("FLYER_Placeholder", c.PLACEHOLDER);
    SW.PAPER       = upsertColor("FLYER_Paper",       [0, 0, 0, 0]);
}
function upsertColor(name, cmyk) {
    var col = doc.colors.itemByName(name);
    if (!col.isValid) {
        col = doc.colors.add({
            name: name,
            model: ColorModel.PROCESS,
            space: ColorSpace.CMYK,
            colorValue: cmyk
        });
    } else {
        /* swatch already exists (re-run) - retune it, but never let a
           locked/reserved swatch abort the whole build. */
        try { col.model = ColorModel.PROCESS; } catch (e) {}
        try { col.space = ColorSpace.CMYK; }    catch (e) {}
        try { col.colorValue = cmyk; }          catch (e) {}
    }
    return col;
}

/* =====================================================================
 * 8. Fonts  -  resolve families with graceful fallback
 * =================================================================== */
function resolveFonts() {
    HF = pickFamily(CONFIG.HEADLINE_FONT);
    BF = pickFamily(CONFIG.BODY_FONT);
    LOG.push("Headline-Font: " + HF + "   |   Body-Font: " + BF);
    try { doc.textDefaults.appliedFont = BF; } catch (e) {}
}
function pickFamily(list) {
    for (var i = 0; i < list.length; i++) {
        if (familyInstalled(list[i])) return list[i];
    }
    LOG.push("WARNUNG: keine der gewuenschten Schriften installiert, nutze '" + list[list.length - 1] + "'.");
    return list[list.length - 1];
}
function familyInstalled(fam) {
    try {
        var f = app.fonts.itemByName(fam);
        if (f.isValid) return true;
    } catch (e) {}
    try {
        var all = app.fonts.everyItem().fontFamily;   /* one bridge call */
        for (var i = 0; i < all.length; i++) {
            if (String(all[i]).toLowerCase() === String(fam).toLowerCase()) return true;
        }
    } catch (e) {}
    return false;
}
function applyFont(style, fam, weights) {
    for (var i = 0; i < weights.length; i++) {
        var f = app.fonts.itemByName(fam + "\t" + weights[i]);
        if (f.isValid && f.status === FontStatus.INSTALLED) {
            try { style.appliedFont = f; return; } catch (e) {}
        }
    }
    try { style.appliedFont = fam; } catch (e) {}   /* family-only fallback */
}

/* =====================================================================
 * 9. Paragraph styles
 *    Headline / Subheadline / Benefit / CTA / Contact / SmallPrint
 * =================================================================== */
function buildParagraphStyles() {

    /* Tracking values kept deliberately modest: wide letter-spacing on
       ALL-CAPS lines was the main driver of the overset text. */
    var h = upsertPara("Headline");
    h.pointSize     = 30;
    h.leading       = 32;
    h.tracking      = 4;
    h.capitalization = Capitalization.ALL_CAPS;
    h.justification = Justification.LEFT_ALIGN;
    h.hyphenation   = false;
    h.spaceAfter    = 0.5;
    h.fillColor     = SW.PRIMARY;
    applyFont(h, HF, CONFIG.HEADLINE_WEIGHTS);

    var s = upsertPara("Subheadline");
    s.pointSize     = 12;
    s.leading       = 15;
    s.tracking      = 8;
    s.capitalization = Capitalization.NORMAL;
    s.justification = Justification.LEFT_ALIGN;
    s.hyphenation   = false;
    s.fillColor     = SW.SECONDARY;
    applyFont(s, BF, CONFIG.BODY_WEIGHTS);

    var b = upsertPara("Benefit");
    b.pointSize     = 12;
    b.leading       = 14;
    b.tracking      = 16;
    b.capitalization = Capitalization.ALL_CAPS;
    b.justification = Justification.LEFT_ALIGN;
    b.hyphenation   = false;
    b.fillColor     = SW.INK;
    applyFont(b, HF, CONFIG.HEADLINE_WEIGHTS);

    var t = upsertPara("CTA");
    t.pointSize     = 11;
    t.leading       = 13;
    t.tracking      = 24;
    t.capitalization = Capitalization.ALL_CAPS;
    t.justification = Justification.CENTER_ALIGN;
    t.hyphenation   = false;
    t.fillColor     = SW.PAPER;
    applyFont(t, HF, CONFIG.HEADLINE_WEIGHTS);

    var c = upsertPara("Contact");
    c.pointSize     = 8.5;
    c.leading       = 11;
    c.tracking      = 3;
    c.capitalization = Capitalization.NORMAL;
    c.justification = Justification.LEFT_ALIGN;
    c.hyphenation   = false;
    c.fillColor     = SW.INK;
    applyFont(c, BF, CONFIG.BODY_WEIGHTS);

    var p = upsertPara("SmallPrint");
    p.pointSize     = 7;
    p.leading       = 8.6;
    p.tracking      = 6;
    p.capitalization = Capitalization.ALL_CAPS;
    p.justification = Justification.LEFT_ALIGN;
    p.hyphenation   = false;
    p.fillColor     = SW.GRAY60;
    applyFont(p, BF, CONFIG.BODY_WEIGHTS);

    LOG.push("Absatzformate: Headline, Subheadline, Benefit, CTA, Contact, SmallPrint");
}
function upsertPara(name) {
    var st = doc.paragraphStyles.itemByName(name);
    if (!st.isValid) st = doc.paragraphStyles.add({ name: name });
    return st;
}

/* =====================================================================
 * 10. Primitive builders
 * =================================================================== */
function rect(page, layerName, bounds, fill, stroke, strokeW) {
    var r = page.rectangles.add({
        itemLayer: L(layerName),
        geometricBounds: bounds
    });
    r.fillColor   = fill   ? fill   : "None";
    r.strokeColor = stroke ? stroke : "None";
    if (strokeW != null) r.strokeWeight = strokeW;
    return r;
}

function textBox(page, layerName, bounds, content, styleName, opts) {
    opts = opts || {};
    var tf = page.textFrames.add({
        itemLayer: L(layerName),
        geometricBounds: bounds
    });
    tf.contents   = content;
    tf.fillColor  = opts.frameFill ? opts.frameFill : "None";
    tf.strokeColor = "None";

    var tfp = tf.textFramePreferences;
    try { tfp.verticalJustification = opts.vj || VerticalJustification.TOP_ALIGN; } catch (e) {}
    try { tfp.insetSpacing = [0, 0, 0, 0]; } catch (e) {}
    /* firstBaselineOffset expects the FirstBaseline enumeration.
       "FirstBaselineOffset" is NOT a global in ExtendScript/InDesign and
       throws ReferenceError. CAP_HEIGHT pins the cap line to the frame top. */
    try { tfp.firstBaselineOffset = FirstBaseline.CAP_HEIGHT; } catch (e) {}

    try { tf.texts[0].appliedParagraphStyle = doc.paragraphStyles.itemByName(styleName); } catch (e) {}
    if (opts.color)   { try { tf.texts[0].fillColor    = opts.color; }   catch (e) {} }
    if (opts.justify) { try { tf.texts[0].justification = opts.justify; } catch (e) {} }
    if (opts.size)    { try { tf.texts[0].pointSize     = opts.size; }    catch (e) {} }
    return tf;
}

/* Rounded accent button: rectangle on GRAPHICS + centred label on TEXT */
function ctaButton(page, bounds, label, fillSwatch, textSwatch) {
    var r = rect(page, "03_GRAPHICS", bounds, fillSwatch, null);
    try { r.cornerOption = CornerOptions.ROUNDED_CORNER; r.cornerRadius = 1.6; } catch (e) {}
    textBox(page, "04_TEXT", bounds, label, "CTA", {
        color: textSwatch,
        vj: VerticalJustification.CENTER_ALIGN,
        justify: Justification.CENTER_ALIGN
    });
}

/* One benefit line: accent square marker + label */
function benefitRow(page, x, y, wRight, label, textSwatch, accentSwatch) {
    var sq = 5;
    var m = rect(page, "03_GRAPHICS", [y, x, y + sq, x + sq], accentSwatch, null);
    try { m.cornerOption = CornerOptions.ROUNDED_CORNER; m.cornerRadius = 0.7; } catch (e) {}
    /* generous frame height so a fallback font that wraps to 2 lines still fits */
    textBox(page, "04_TEXT", [y - 1.6, x + sq + 4, y + sq + 3.4, wRight], label, "Benefit",
            { color: textSwatch, vj: VerticalJustification.CENTER_ALIGN });
}

/* =====================================================================
 * 11. Image placement with safe fallback
 *   fileOrPath : a File object OR a path string OR null
 *   role       : "windowHero" | "carHero" | "logo" | "qr" | undefined
 *                heroes are REQUIRED, logo/QR are OPTIONAL
 * =================================================================== */
function placeOrPlaceholder(page, layerName, bounds, fileOrPath, label, mood, fitMode, bigLabel, role) {
    var r = rect(page, layerName, bounds, null, null);

    var f = null;
    if (fileOrPath) {
        f = (fileOrPath instanceof File) ? fileOrPath : new File(resolvePath(String(fileOrPath)));
    }

    if (f && f.exists) {
        try {
            r.place(f);
            r.fit(fitMode || FitOptions.FILL_PROPORTIONALLY);
            r.fit(FitOptions.CENTER_CONTENT);
            r.strokeColor = "None";
            if (role && PLACED.hasOwnProperty(role)) PLACED[role] = true;
            LOG.push("Bild platziert (" + label + "): " + f.fsName);
            return r;
        } catch (e) {
            LOG.push("Platzieren fehlgeschlagen (" + label + "): " + e);
            recordMissing(role, label, f.fsName);
        }
    } else {
        recordMissing(role, label, f ? f.fsName : "(kein Pfad gesetzt)");
    }
    drawPlaceholder(r, label, mood, page, bounds, bigLabel);
    return r;
}

/* Route a missing/failed asset into the right severity bucket. */
function recordMissing(role, label, path) {
    var entry = label + "  ->  " + path;
    MISSING.push(entry);                                   /* legacy aggregate */
    if (role === "logo" || role === "qr") MISS_OPT.push(entry);
    else                                  MISS_REQ.push(entry);
    LOG.push((role === "logo" || role === "qr" ? "OPTIONAL fehlt: " : "PFLICHT fehlt: ") + entry);
}

/* Log presence of the design-reference images. They are never placed. */
function noteReferences() {
    var w = assetFile("", CONFIG.WINDOW_REF_FILE);
    var c = assetFile("", CONFIG.CAR_REF_FILE);
    LOG.push("Referenz Fensterfolierung.png: " + (w && w.exists ? "vorhanden (nur Design-Vorlage, nicht platziert)" : "nicht gefunden"));
    LOG.push("Referenz Autofolierung.png: "    + (c && c.exists ? "vorhanden (nur Design-Vorlage, nicht platziert)" : "nicht gefunden"));
}

function drawPlaceholder(r, label, mood, page, bounds, big) {
    r.fillColor   = SW.PLACEHOLDER;
    r.strokeColor = (mood === "dark") ? SW.LIGHT_TEXT : SW.GRAY60;
    r.strokeWeight = 0.75;
    try { r.strokeType = doc.strokeStyles.itemByName("Dashed"); } catch (e) {}

    var t = textBox(page, r.itemLayer.name, bounds, label, "SmallPrint", {
        vj: VerticalJustification.CENTER_ALIGN,
        justify: Justification.CENTER_ALIGN,
        color: (mood === "dark") ? SW.LIGHT_TEXT : SW.GRAY60,
        size: big ? 11 : 7
    });
    return t;
}

function resolvePath(p) {
    /* expand leading ~ to the user home folder */
    if (p && p.charAt(0) === "~") return Folder("~").fsName + p.substring(1);
    return p;
}

/* =====================================================================
 * 12. Branding block  (logo / name / contact / QR)  -  identical layout
 *     on both pages, only the mood (colour) changes.
 * =================================================================== */
function brandingBlock(page, mood) {
    var textSwatch = (mood === "dark") ? SW.LIGHT_TEXT : SW.INK;
    var lineSwatch = (mood === "dark") ? SW.GRAY60     : SW.SECONDARY;

    /* Identical geometry on both pages (only the mood colour differs).
       The block runs 190.4 .. 212.5 mm - inside the 3 mm bleed. */
    var NX = CL + 33;          /* text left  (43 mm)  */
    var TR = BX_R - 24;        /* text right (127 mm) */

    /* divider - a hairline rule. Drawn as a thin rectangle: a zero-height
       graphicLine geometricBounds is rejected by some InDesign builds. */
    try {
        var dv = rect(page, "03_GRAPHICS", [190.4, CL, 190.7, CR], lineSwatch, null);
        dv.strokeColor = "None";
    } catch (e) { LOG.push("Divider konnte nicht gezeichnet werden: " + e); }

    /* logo (left) - OPTIONAL */
    placeOrPlaceholder(page, "05_BRANDING", [191.5, CL, 206.5, CL + 28],
                       assetFile(CONFIG.LOGO_PATH, CONFIG.LOGO_FILE),
                       "LOGO (optional)", mood, FitOptions.PROPORTIONALLY, false, "logo");

    /* company name (centre, wide) */
    textBox(page, "05_BRANDING", [191, NX, 197.5, TR],
            CONFIG.CONTACT.companyName, "Contact",
            { color: textSwatch, size: 9 });

    /* contact details - wide, tall frame so no fallback font can overset */
    var c = CONFIG.CONTACT;
    var block = c.phone + "   " + c.email + "\r" +
                c.website + "   " + c.instagram;
    textBox(page, "05_BRANDING", [198, NX, 212.5, TR],
            block, "SmallPrint", { color: textSwatch });

    /* QR (right) - OPTIONAL */
    placeOrPlaceholder(page, "05_BRANDING", [190.5, BX_R - 22, 208.5, BX_R - 4],
                       assetFile(CONFIG.QR_PATH, CONFIG.QR_FILE),
                       "QR (optional)", mood, FitOptions.PROPORTIONALLY, false, "qr");
}

/* =====================================================================
 * 13. PAGE 1  -  WINDOW FILMS  (bright, architectural, trustworthy)
 * =================================================================== */
function buildPage1() {
    var pg = doc.pages[0];

    /* 01 background */
    rect(pg, "01_BACKGROUND", [BX_T, BX_L, BX_B, BX_R], SW.BG_LIGHT, null);

    /* 03 top brand band + headline rule */
    rect(pg, "03_GRAPHICS", [BX_T, BX_L, 5, BX_R], SW.PRIMARY, null);
    rect(pg, "03_GRAPHICS", [43, CL, 44.4, 58], SW.ACCENT, null);

    /* 02 hero image - lower half, full bleed left / right / bottom.
       REQUIRED asset: Fensterfolierung2.png (next to the script). */
    placeOrPlaceholder(pg, "02_IMAGES", [96, BX_L, BX_B, BX_R],
                       assetFile(CONFIG.WINDOW_IMAGE_PATH, CONFIG.WINDOW_IMAGE_FILE),
                       "HERO FENSTERFOLIE (Fensterfolierung2.png)",
                       "light", FitOptions.FILL_PROPORTIONALLY, true, "windowHero");

    /* 03 solid footer panel so branding stays readable over the photo */
    rect(pg, "03_GRAPHICS", [189, BX_L, BX_B, BX_R], SW.BG_LIGHT, null);

    /* 04 text */
    textBox(pg, "04_TEXT", [12, CL, 42, CR],
            "FENSTERFOLIERUNGEN", "Headline", { color: SW.PRIMARY });

    textBox(pg, "04_TEXT", [44, CL, 58, CR],
            "Mehr Schutz. Mehr Privatsphäre. Mehr Komfort.",
            "Subheadline", { color: SW.SECONDARY });

    benefitRow(pg, CL, 63, CR, "SONNENSCHUTZ", SW.INK, SW.ACCENT);
    benefitRow(pg, CL, 74, CR, "SICHTSCHUTZ",  SW.INK, SW.ACCENT);
    benefitRow(pg, CL, 85, CR, "UV-SCHUTZ",    SW.INK, SW.ACCENT);

    ctaButton(pg, [166, 16, 182, 132],
              "Jetzt unverbindlich beraten lassen", SW.ACCENT, SW.PAPER);

    /* 05 branding */
    brandingBlock(pg, "light");
}

/* =====================================================================
 * 14. PAGE 2  -  CAR WRAPPING  (dark, dynamic, premium, automotive)
 * =================================================================== */
function buildPage2() {
    var pg = doc.pages[1];

    /* 01 background */
    rect(pg, "01_BACKGROUND", [BX_T, BX_L, BX_B, BX_R], SW.BG_DARK, null);

    /* 02 hero image - upper half, full bleed top / left / right.
       REQUIRED asset: Autofolierung2.png (next to the script). */
    placeOrPlaceholder(pg, "02_IMAGES", [BX_T, BX_L, 98, BX_R],
                       assetFile(CONFIG.CAR_IMAGE_PATH, CONFIG.CAR_IMAGE_FILE),
                       "HERO AUTOFOLIE (Autofolierung2.png)",
                       "dark", FitOptions.FILL_PROPORTIONALLY, true, "carHero");

    /* 03 accent bar above headline (mirrors page 1 rule) */
    rect(pg, "03_GRAPHICS", [103, CL, 105, 44], SW.ACCENT, null);

    /* 04 text - light on dark */
    textBox(pg, "04_TEXT", [107, CL, 132, CR],
            "AUTOFOLIERUNGEN", "Headline", { color: SW.PAPER });

    textBox(pg, "04_TEXT", [133, CL, 145, CR],
            "Neuer Look. Geschützter Lack. Deine Farbe.",
            "Subheadline", { color: SW.LIGHT_TEXT });

    benefitRow(pg, CL, 147, CR, "INDIVIDUELLES DESIGN", SW.PAPER, SW.ACCENT);
    benefitRow(pg, CL, 158, CR, "LACKSCHUTZ",           SW.PAPER, SW.ACCENT);
    benefitRow(pg, CL, 169, CR, "RÜCKRÜSTBAR",          SW.PAPER, SW.ACCENT);

    ctaButton(pg, [178, 16, 189, 132],
              "Jetzt Termin anfragen", SW.ACCENT, SW.PAPER);

    /* 05 branding */
    brandingBlock(pg, "dark");
}

/* =====================================================================
 * 15. Save / export
 * =================================================================== */
function outputFolder() {
    /* "" -> the folder that holds the script; otherwise the configured path */
    if (CONFIG.OUTPUT_FOLDER && String(CONFIG.OUTPUT_FOLDER).length) {
        return new Folder(resolvePath(String(CONFIG.OUTPUT_FOLDER)));
    }
    return SCRIPT_DIR;
}

function finishOutput() {
    if (CONFIG.SAVE_INDD) {
        try {
            var folder = outputFolder();
            if (!folder.exists) folder.create();
            var out = new File(folder.fsName + "/" + CONFIG.INDD_NAME);
            doc.save(out);
            SAVED = true;
            LOG.push("Gespeichert: " + out.fsName);
        } catch (e) {
            LOG.push("Speichern fehlgeschlagen: " + e);
        }
    } else {
        LOG.push("Speichern deaktiviert (CONFIG.SAVE_INDD = false).");
    }

    if (CONFIG.EXPORT_PDF) {
        try {
            var preset = app.pdfExportPresets.itemByName(CONFIG.PDF_PRESET);
            if (!preset.isValid) {
                LOG.push("PDF-Preset nicht gefunden: " + CONFIG.PDF_PRESET + " - Export uebersprungen.");
            } else {
                var p = app.pdfExportPreferences;
                p.useDocumentBleedWithPDF = true;
                p.cropMarks             = true;
                p.bleedMarks            = false;
                p.registrationMarks     = true;
                p.colorBars             = true;
                p.pageInformationMarks  = true;
                p.pageMarksOffset       = BLEED;

                var pdfFolder = outputFolder();
                if (!pdfFolder.exists) pdfFolder.create();
                var pdf = new File(pdfFolder.fsName + "/" + CONFIG.PDF_NAME);
                doc.exportFile(ExportFormat.PDF_TYPE, pdf, false, preset);
                LOG.push("PDF exportiert: " + pdf.fsName);
            }
        } catch (e) {
            LOG.push("PDF-Export fehlgeschlagen: " + e);
        }
    }
}

/* =====================================================================
 * 15b. Overset healing
 *   Layout + typography above is sized so text fits with the intended
 *   fonts. This pass is the guarantee: for any frame still reporting
 *   overflow (e.g. a very wide substitute font), shrink type in 0.5 pt
 *   steps down to a per-style floor, then tighten leading a touch.
 *   Body floor 7.5 pt, small print 6.5 pt, headline 20 pt - hierarchy kept.
 * =================================================================== */
function styleFloor(name) {
    if (name === "Headline")   return 20;
    if (name === "Subheadline") return 9;
    if (name === "Benefit")    return 8.5;
    if (name === "CTA")        return 8.5;
    if (name === "SmallPrint") return 6.5;
    if (name === "Contact")    return 7.5;
    return 7.5;
}

function healFrame(tf) {
    if (!tf || !tf.isValid) return true;
    var guard = 0;
    try {
        if (!tf.overflows) return true;

        var story = tf.parentStory;
        var t     = story.texts[0];
        var floor = 7.5;
        try { floor = styleFloor(String(t.appliedParagraphStyle.name)); } catch (e) {}

        while (tf.overflows && guard < 40) {
            guard++;
            var cur = t.pointSize;
            if (typeof cur !== "number" || isNaN(cur)) cur = 9;

            if (cur - 0.5 >= floor) {
                t.pointSize = cur - 0.5;
                try { t.leading = (cur - 0.5) * 1.12; } catch (eL) {}
                continue;
            }
            /* at the floor - try a last small leading squeeze, then give up */
            var ld = t.leading;
            if (typeof ld === "number" && ld > floor * 1.02) {
                t.leading = Math.max(floor * 1.02, ld - 0.5);
                continue;
            }
            break;
        }
    } catch (e) {
        LOG.push("healFrame Fehler: " + e);
    }
    return !tf.overflows;
}

function healOverset() {
    var frames = doc.textFrames.everyItem().getElements();
    var healed = 0, still = 0;
    for (var i = 0; i < frames.length; i++) {
        var f = frames[i];
        if (!f.isValid || !f.overflows) continue;
        if (healFrame(f)) healed++;
        else {
            still++;
            var pn = f.parentPage ? ("Seite " + f.parentPage.name) : "Musterseite";
            LOG.push("UEBERSATZ NICHT behoben auf " + pn);
        }
    }
    LOG.push("Ubersatz-Heilung: " + healed + " Rahmen angepasst, " + still + " weiterhin uebervoll.");
}

/* =====================================================================
 * 16. Validation report
 * =================================================================== */
function report() {
    var lines = [];
    lines.push("======  FLYER BUILDER  -  VALIDIERUNG  ======");

    /* dimensions / bleed / pages */
    var dp = doc.documentPreferences;
    var a5OK     = Math.round(dp.pageWidth) === PAGE_W && Math.round(dp.pageHeight) === PAGE_H;
    var bleedOK  = dp.documentBleedTopOffset === BLEED && dp.documentBleedBottomOffset === BLEED &&
                   dp.documentBleedInsideOrLeftOffset === BLEED && dp.documentBleedOutsideOrRightOffset === BLEED;
    var pagesOK  = doc.pages.length === 2;
    var facingOK = dp.facingPages === false;
    lines.push(chk(a5OK,     "A5 148 x 210 mm"));
    lines.push(chk(bleedOK,  "3 mm Bleed auf allen Seiten"));
    lines.push(chk(pagesOK,  "2 Seiten"));
    lines.push(chk(facingOK, "Facing Pages AUS"));

    /* overset text */
    var over = [];
    var frames = doc.textFrames.everyItem().getElements();
    for (var i = 0; i < frames.length; i++) {
        if (frames[i].overflows) {
            var pn = frames[i].parentPage ? ("Seite " + frames[i].parentPage.name) : "Musterseite";
            over.push(pn);
        }
    }
    lines.push(chk(over.length === 0, "Kein Ubersatztext" + (over.length ? " (betroffen: " + over.join(", ") + ")" : "")));

    /* missing links */
    var broken = [];
    try {
        var lk = doc.links.everyItem().getElements();
        for (var j = 0; j < lk.length; j++) {
            if (lk[j].status === LinkStatus.LINK_MISSING) broken.push(lk[j].name);
        }
    } catch (e) {}
    lines.push(chk(broken.length === 0, "Keine fehlenden Verknupfungen" +
                   (broken.length ? " (" + broken.join(", ") + ")" : "")));

    /* image resolution */
    var lowRes = [];
    try {
        var gr = doc.allGraphics;
        for (var g = 0; g < gr.length; g++) {
            try {
                var ppi = gr[g].effectivePpi;   /* [h, v] - not present on vector art */
                if (ppi && ppi.length && Math.min(ppi[0], ppi[1]) > 0 && Math.min(ppi[0], ppi[1]) < 220) {
                    lowRes.push(gr[g].itemLink ? gr[g].itemLink.name : "Bild " + (g + 1));
                }
            } catch (eppi) {}
        }
    } catch (e) {}
    lines.push(chk(lowRes.length === 0, "Bildauflosung >= 220 ppi effektiv" +
                   (lowRes.length ? " (niedrig: " + lowRes.join(", ") + ")" : "")));

    /* colour space */
    var rgb = [];
    try {
        var cols = doc.colors.everyItem().getElements();
        for (var k = 0; k < cols.length; k++) {
            var nm = String(cols[k].name);
            if (nm.indexOf("FLYER_") === 0 && cols[k].space !== ColorSpace.CMYK) rgb.push(nm);
        }
    } catch (e) {}
    lines.push(chk(rgb.length === 0, "Flyer-Farben im CMYK-Raum"));

    /* ---- assets: split by severity (Pflicht vs. Optional) ---- */
    var heroOK = PLACED.windowHero && PLACED.carHero;
    var heroMiss = (!PLACED.windowHero ? "Fenster " : "") + (!PLACED.carHero ? "Auto" : "");
    lines.push(chk(heroOK, "PFLICHT: Hero-Bilder platziert (Fensterfolierung2.png + Autofolierung2.png)" +
                   (heroOK ? "" : "  -> fehlt: " + heroMiss)));

    /* saved? (mandatory unless the user disabled saving on purpose) */
    var saveOK = CONFIG.SAVE_INDD ? SAVED : true;
    lines.push(chk(saveOK, CONFIG.SAVE_INDD ? "PFLICHT: Dokument gespeichert"
                                            : "PFLICHT: Speichern per CONFIG deaktiviert (uebersprungen)"));

    var cc = CONFIG.CONTACT;
    var contactPlaceholder = /muster/i.test(String(cc.companyName)) ||
                             /muster/i.test(String(cc.email)) ||
                             /0\s*00\s*00\s*000/.test(String(cc.phone));

    lines.push("");
    lines.push("--- OPTIONALES BRANDING (kein Pflicht-Fehler) ---");
    lines.push(chkOpt(PLACED.logo,            "Logo-Asset vorhanden (logo.png)"));
    lines.push(chkOpt(PLACED.qr,              "QR-Asset vorhanden (qr.png)"));
    lines.push(chkOpt(!contactPlaceholder,    "Echte Kontaktdaten hinterlegt (nicht Musterdaten)"));

    /* ---- overall mandatory verdict ---- */
    var mandatoryOK = a5OK && bleedOK && pagesOK && facingOK &&
                      heroOK && (over.length === 0) &&
                      (broken.length === 0) && (rgb.length === 0) && saveOK;

    lines.push("");
    lines.push("=====================================================");
    lines.push(mandatoryOK
        ? "GESAMT-STATUS:  PASS  -  alle Pflichtkriterien erfuellt, druckbereit."
        : "GESAMT-STATUS:  FAIL  -  Pflichtkriterien NICHT erfuellt (siehe [!!]).");
    lines.push("=====================================================");
    if (!PLACED.logo || !PLACED.qr || contactPlaceholder) {
        lines.push("HINWEIS (nicht kritisch) - Branding noch unvollstaendig:");
        if (!PLACED.logo)        lines.push("   - Logo nicht geliefert (Platzhalter im Layout)");
        if (!PLACED.qr)          lines.push("   - QR nicht geliefert (Platzhalter im Layout)");
        if (contactPlaceholder)  lines.push("   - Kontaktdaten noch Musterdaten (CONFIG.CONTACT anpassen)");
    }

    lines.push("");
    lines.push("--- PDF-EXPORT-BEREITSCHAFT ---");
    lines.push(chk(mandatoryOK,
                   "PDF/X-4 Export-Bereitschaft (nur bei erfuellten Pflichtkriterien)"));
    lines.push("Manuell:  Datei > Exportieren > Adobe PDF (Druck) > " + CONFIG.PDF_PRESET);
    lines.push("          Marken & Anschnitt > Dokument-Anschnitt verwenden.");

    if (MISS_REQ.length) {
        lines.push("");
        lines.push("--- OFFENE PFLICHT-PLATZHALTER (blockieren PASS) ---");
        for (var mr = 0; mr < MISS_REQ.length; mr++) lines.push("  - " + MISS_REQ[mr]);
    }
    if (MISS_OPT.length) {
        lines.push("");
        lines.push("--- OFFENE OPTIONALE PLATZHALTER (nur Hinweis) ---");
        for (var mo = 0; mo < MISS_OPT.length; mo++) lines.push("  - " + MISS_OPT[mo]);
    }

    lines.push("");
    lines.push("--- LOG ---");
    for (var p = 0; p < LOG.length; p++) lines.push("  " + LOG[p]);

    var out = lines.join("\n");
    try { $.writeln(out); } catch (e) {}
    alert(out);
}
function chk(ok, label)    { return (ok ? "[OK]   " : "[!!]   ") + label; }
function chkOpt(ok, label) { return (ok ? "[OK]   " : "[WARN] ") + label; }

/* =====================================================================
 * RUN
 * =================================================================== */
main();
