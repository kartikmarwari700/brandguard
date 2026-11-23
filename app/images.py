"""Visual identity and accessibility checks for images (improvement I3).

Code does everything that can be measured: the image's main colours against the brand palette (CIELAB
distance), text contrast against WCAG 2.1 (4.5:1, or 3:1 for large text) and whether alt text exists and
says something. The AI is used only to look: it reads the text in the image with its position, and
writes a suggested alt text. The text it reads then goes through the same rule and fact checks as any post.
"""
import base64
import copy
import hashlib
import io
import json
import math
import re
from pathlib import Path

from . import agent, engine

MAX_UPLOAD = 4_000_000
MAX_SIDE = 1024
ALLOWED = {"PNG": "PNG", "JPEG": "JPG", "WEBP": "WebP"}
OFF_PALETTE_DELTA_E = 20     # CIE76 distance; above this a colour is not a brand colour
MIN_COLOUR_SHARE = 0.08      # ignore colours covering less than 8% of the image
GENERIC_ALT = {"image", "img", "picture", "photo", "poster", "banner", "graphic", "logo", "screenshot", "post", "flyer"}

_cache = {}


def _err(msg):
    from .store import WorkflowError
    return WorkflowError(msg)


# ---------------------------------------------------------------- loading

def load_upload(data, alt=""):
    from PIL import Image, ImageOps, UnidentifiedImageError

    if not data:
        raise _err("The image file is empty.")
    if len(data) > MAX_UPLOAD:
        raise _err("The image is larger than 4 MB. Please upload a smaller PNG, JPG or WebP.")
    try:
        img = Image.open(io.BytesIO(data))
        fmt = img.format
        img.load()
    except (UnidentifiedImageError, OSError, ValueError):
        raise _err("This file is not a readable PNG, JPG or WebP image.")
    if fmt not in ALLOWED:
        raise _err(f"{fmt or 'This'} images are not supported. Please upload PNG, JPG or WebP.")
    img = ImageOps.exif_transpose(img).convert("RGB")
    if min(img.size) < 32:
        raise _err("The image is too small to check (at least 32 × 32 pixels).")
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    stored = buf.getvalue()
    thumb = img.copy()
    thumb.thumbnail((240, 240))
    tbuf = io.BytesIO()
    thumb.save(tbuf, "JPEG", quality=80)
    return {"sha": hashlib.sha256(stored).hexdigest()[:16], "mime": "image/jpeg", "width": img.width, "height": img.height,
            "data": base64.b64encode(stored).decode(), "alt": (alt or "").strip()[:300],
            "thumb": "data:image/jpeg;base64," + base64.b64encode(tbuf.getvalue()).decode()}


def load_seed_image(name, alt):
    return load_upload((Path(__file__).with_name("seed") / name).read_bytes(), alt)


def _pil(media):
    from PIL import Image

    return Image.open(io.BytesIO(base64.b64decode(media["data"]))).convert("RGB")


# ---------------------------------------------------------------- colour maths

def hex_to_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def rgb_to_hex(rgb):
    return "#{:02X}{:02X}{:02X}".format(*rgb)


def _lab(rgb):
    def lin(c):
        c /= 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(c) for c in rgb)
    x, y, z = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047, 0.2126 * r + 0.7152 * g + 0.0722 * b, \
        (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883
    f = lambda t: t ** (1 / 3) if t > 216 / 24389 else (24389 / 27 * t + 16) / 116  # noqa: E731
    fx, fy, fz = f(x), f(y), f(z)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def delta_e(a, b):
    return math.dist(_lab(a), _lab(b))


def luminance(rgb):
    def ch(c):
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (ch(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def dominant_colours(img, k=6):
    from PIL import Image

    small = img.copy()
    small.thumbnail((160, 160))
    quant = small.quantize(colors=k, method=Image.Quantize.MEDIANCUT)
    pal = quant.getpalette()
    total = small.width * small.height
    out = []
    for count, idx in sorted(quant.getcolors(), reverse=True):
        rgb = tuple(pal[idx * 3: idx * 3 + 3])
        merged = next((o for o in out if delta_e(o["rgb"], rgb) < 6), None)
        if merged:
            merged["share"] += count / total
        else:
            out.append({"rgb": rgb, "share": count / total})
    for o in out:
        o["hex"], o["share"] = rgb_to_hex(o["rgb"]), round(o["share"], 3)
    return out


def _neutral(rgb):
    _, a, b = _lab(rgb)
    return math.hypot(a, b) < 8  # white, black and greys are allowed with any palette


# ---------------------------------------------------------------- code checks (run when the version is created)

def _f(rule_id, category, severity, quote, message, target, fix=None, fix_note=None, extra=None):
    f = engine._finding(rule_id, category, severity, 0, 0, quote, message, fix=fix, fix_note=fix_note)
    f["target"] = target
    f.update(extra or {})
    return f


def check_image(media, kit):
    img = _pil(media)
    palette = [{"name": p["name"], "hex": p["hex"], "rgb": hex_to_rgb(p["hex"])} for p in kit["brand"].get("palette", [])]
    findings = []
    colours = dominant_colours(img)
    media["colours"] = []
    for c in colours:
        if c["share"] < MIN_COLOUR_SHARE:
            continue
        near = min(palette, key=lambda p: delta_e(p["rgb"], c["rgb"])) if palette else None
        de = delta_e(near["rgb"], c["rgb"]) if near else 0
        ok = _neutral(c["rgb"]) or (near and de <= OFF_PALETTE_DELTA_E)
        media["colours"].append({"hex": c["hex"], "share": c["share"], "ok": bool(ok),
                                 "nearest": near and {"name": near["name"], "hex": near["hex"], "delta_e": round(de, 1)}})
        if not ok:
            findings.append(_f("VIS-01", "visual", "major", c["hex"],
                               f"Off-palette colour {c['hex']} covers {round(c['share'] * 100)}% of the image. The nearest brand colour is "
                               f"{near['name']} {near['hex']} (colour distance ΔE {de:.0f}; brand colours are within {OFF_PALETTE_DELTA_E}).",
                               "image", fix_note=f"Use a brand colour instead, for example {near['name']} {near['hex']}.",
                               extra={"swatch": c["hex"], "suggest": near["hex"]}))
    alt = (media.get("alt") or "").strip()
    words = re.findall(r"[A-Za-z0-9']+", alt)
    if not alt:
        findings.append(_f("ACC-04", "accessibility", "major", "", "The image has no alt text. Screen-reader users will not know what "
                           "it shows or what it says.", "alt", fix_note="Describe what the image shows and any text in it."))
    elif len(words) < 4 or alt.lower().strip(" .") in GENERIC_ALT or re.search(r"\.(png|jpe?g|webp)$", alt, re.I):
        findings.append(_f("ACC-04", "accessibility", "minor", alt, f"The alt text “{alt}” does not describe the image. "
                           "Say what it shows and include any text in it.", "alt", fix_note="Describe what the image shows and any text in it."))
    return findings


# ---------------------------------------------------------------- AI look + code measurement

READ_TOOL = {
    "name": "report_image", "strict": True,
    "description": "Report the text visible in the image with its position, and a suggested alt text. Call exactly once.",
    "input_schema": {"type": "object", "additionalProperties": False, "required": ["texts", "alt_text"],
                     "properties": {
                         "texts": {"type": "array", "description": "Each separate block of visible text, top to bottom",
                                   "items": {"type": "object", "additionalProperties": False,
                                             "required": ["text", "x", "y", "w", "h", "size"],
                                             "properties": {
                                                 "text": {"type": "string", "description": "Exactly as written"},
                                                 "x": {"type": "number", "description": "Left edge, 0 to 1 of image width"},
                                                 "y": {"type": "number", "description": "Top edge, 0 to 1 of image height"},
                                                 "w": {"type": "number", "description": "Width, 0 to 1 of image width"},
                                                 "h": {"type": "number", "description": "Height, 0 to 1 of image height"},
                                                 "size": {"type": "string", "enum": ["large", "normal"]}}}},
                         "alt_text": {"type": "string", "description": "One or two sentences for a screen reader: what the image shows, "
                                                                       "including its key text. No 'image of'."}}},
}

LOOK_PROMPT = """You help check marketing images before publishing. Look at the image and call report_image once.
List every separate block of visible text exactly as written, with a tight bounding box (fractions of the image width and
height) and whether it is large text (headline size). Then write a helpful alt text. Do not judge the content."""


def measure(img, box, size):
    """Text vs background colour inside the box: the two main colours, the smaller share being the text."""
    from PIL import Image

    W, H = img.size
    x0, y0 = max(0, int(box["x"] * W)), max(0, int(box["y"] * H))
    x1, y1 = min(W, int((box["x"] + box["w"]) * W)), min(H, int((box["y"] + box["h"]) * H))
    if x1 - x0 < 6 or y1 - y0 < 6:
        return None
    crop = img.crop((x0, y0, x1, y1))
    crop.thumbnail((1000, 1000))  # keep full resolution: shrinking blends thin strokes into the background
    # background = the most common colour; text = a clearly different colour cluster (anti-aliased edge
    # pixels form small in-between clusters)
    q = crop.quantize(colors=8, method=Image.Quantize.FASTOCTREE)
    pal, total = q.getpalette(), crop.width * crop.height
    counts = sorted(q.getcolors(), reverse=True)
    colours = [(n / total, tuple(pal[i * 3:i * 3 + 3])) for n, i in counts]
    bg = colours[0][1]
    candidates = [(share, rgb) for share, rgb in colours[1:] if share >= 0.02 and delta_e(rgb, bg) > 12]
    if not candidates:
        return None
    # thin strokes are mostly blended pixels, so prefer the most contrasting cluster that is still a real part of the text
    largest = max(share for share, _ in candidates)
    fg = max((c for c in candidates if c[0] >= largest * 0.25), key=lambda c: contrast(c[1], bg))[1]
    ratio = contrast(fg, bg)
    need = 3.0 if size == "large" else 4.5
    return {"fg": rgb_to_hex(fg), "bg": rgb_to_hex(bg), "ratio": round(ratio, 2), "need": need, "pass": ratio >= need}


def _best_text_colour(bg_hex, kit):
    bg = hex_to_rgb(bg_hex)
    opts = [(contrast(hex_to_rgb(p["hex"]), bg), p) for p in kit["brand"].get("palette", [])]
    if not opts:
        return None
    ratio, p = max(opts, key=lambda o: o[0])
    return f"{p['name']} {p['hex']} ({ratio:.1f}:1)"


def _image_text_findings(text, kit):
    findings, verified = engine.run_rule_checks(text, kit, None) if text.strip() else ([], [])
    # capitals are a design choice in images: skip ALL-CAPS and case-only spelling differences
    findings = [f for f in findings if f["rule_id"] != "ACC-03" and not (
        f["rule_id"].startswith("TERM") and f.get("fix") and f["quote"].lower() == f["fix"].lower())]
    for f in findings:
        f["target"] = "image_text"
        if f.get("fix") is not None:
            change = f"“{f['quote']}” → “{f['fix']}”" if f["fix"] else f"remove “{f['quote']}”"
            f["fix_note"] = f"Change it in the design: {change}." + (f" {f['fix_note']}" if f.get("fix_note") else "")
            f["fix"] = None
    return findings, verified


def _cta_finding(intent, caption, image_text, kit):
    found = engine.check_cta((caption or "") + "\n" + (image_text or ""), intent, kit)
    for f in found:
        f["target"] = "text"
        f["start"] = f["end"] = len(caption or "")
    return found


def _offline(media, intent, kit, caption, note):
    return {"mode": "rules-only", "status": "done", "note": note, "findings": _cta_finding(intent, caption, "", kit),
            "rewrite": None, "rewrite_check": None, "tool_log": [], "rejected": [], "cost_usd": 0.0,
            "image_text": "", "elements": [], "image_verified": [], "alt_suggestion": None}


def review_image(media, intent, kit, caption, caption_findings):
    if not agent.ai_available():
        return _offline(media, intent, kit, caption, "Reading the text in the image and measuring its contrast need the AI "
                        "review, which is off. Colours and alt text were checked in code.")
    key = hashlib.sha256(json.dumps([agent.MODEL, media["sha"], media.get("alt"), caption, intent,
                                     {k: v for k, v in kit.items() if k != "version"}], sort_keys=True).encode()).hexdigest()
    if key in _cache:
        agent.budget.count_reuse()
        return {**copy.deepcopy(_cache[key]), "cached": True, "cost_usd": 0.0}
    blocked = agent.budget.try_start()
    if blocked:
        return _offline(media, intent, kit, caption, blocked)
    import anthropic

    try:
        response = agent.make_client().messages.create(
            model=agent.MODEL, max_tokens=3000, system=LOOK_PROMPT, tools=[READ_TOOL],
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media["mime"], "data": media["data"]}},
                {"type": "text", "text": "Report the text in this image and a suggested alt text."}]}])
    except (anthropic.APIStatusError, anthropic.APIConnectionError) as exc:
        return _offline(media, intent, kit, caption, f"AI image review unavailable ({agent.api_error(exc)}); "
                        "colours and alt text were checked in code.")
    cost = agent.budget.add(agent.MODEL, response.usage)
    call = next((b for b in response.content if b.type == "tool_use" and b.name == "report_image"), None)
    if not call:
        return {**_offline(media, intent, kit, caption, "The AI did not read the image; colours and alt text were checked in code."),
                "cost_usd": round(cost, 5)}
    img = _pil(media)
    elements, findings = [], []
    for t in (call.input.get("texts") or [])[:25]:
        text = str(t.get("text", "")).strip()
        if not text:
            continue
        box = {k: min(1.0, max(0.0, float(t.get(k, 0)))) for k in ("x", "y", "w", "h")}
        m = measure(img, box, t.get("size"))
        elements.append({"text": text, "box": box, "size": t.get("size"), "measure": m})
        if m and not m["pass"]:
            best = _best_text_colour(m["bg"], kit)
            findings.append(_f("ACC-05", "accessibility", "major", text[:80],
                               f"Text contrast is {m['ratio']}:1 (text {m['fg']} on {m['bg']}); WCAG needs at least {m['need']}:1 "
                               f"for {'large' if m['need'] == 3.0 else 'normal'} text.", "image",
                               fix_note=f"Use a darker or lighter text colour, for example {best}." if best else None,
                               extra={"box": box, "swatch": m["fg"], "suggest": m["bg"]}))
    image_text = "\n".join(e["text"] for e in elements)
    text_findings, image_verified = _image_text_findings(image_text, kit)
    findings += text_findings + _cta_finding(intent, caption, image_text, kit)
    alt = str(call.input.get("alt_text") or "").strip()[:300] or None
    result = {"mode": "ai", "model": agent.MODEL, "status": "done", "note": None, "findings": findings, "rewrite": None,
              "rewrite_check": None, "rejected": [], "cost_usd": round(cost, 5), "cached": False,
              "tool_log": [{"tool": "report_image", "input": {"texts": len(elements), "alt_text": alt or ""}, "ok": True}],
              "image_text": image_text, "elements": elements, "image_verified": image_verified, "alt_suggestion": alt}
    _cache[key] = copy.deepcopy(result)
    return result
