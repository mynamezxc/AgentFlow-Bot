"""Image generation: engine resolution, Codex imagegen, Gemini API, OpenAI-compatible gateway, artifacts.

Resolution (settings `image`):
  mode 'agent' (default): the generating agent's own provider -> codex: Codex imagegen with that agent's model;
                          gemini: Gemini API image model; claude/gateway: `fallback` list, then the first available
                          engine (Codex signed in -> Gemini API key -> gateway image model).
  mode 'fixed'          : always the engine/model chosen in Settings.
"""
import base64
import hashlib
import ipaddress
import json
import re
import shutil
import socket
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from .accounts import AccountUnavailable, ProviderLimit
from .research import PinnedConnection
from .store import DomainError, uid

ENGINE_IDS = ("codex", "gemini", "gateway")
LABELS = {"codex": "Codex (built-in image generation)", "gemini": "Gemini API (Nano Banana)", "gateway": "OpenAI-compatible gateway"}
ASPECTS = ("1:1", "3:2", "2:3", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9")
SIZES = ("auto", "512px", "1K", "2K", "4K")
QUALITIES = ("auto", "low", "medium", "high")
FORMATS = ("png", "jpeg", "webp")
BACKGROUNDS = ("auto", "transparent", "opaque")
DEFAULTS = {"aspect_ratio": "1:1", "size": "1K", "quality": "auto", "format": "png", "count": 1, "style": "none"}
MAX_PROMPT = 10000
MAX_COUNT = 4
TIMEOUT = 300
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp"}
MAX_IMAGE = 15 * 1024 * 1024
MAX_FILE = 20 * 1024 * 1024
GEMINI_REF_CAP = 7 * 1024 * 1024
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
CATALOG_TTL = 3600
CODEX_NOTICE = "Codex image generation uses your Codex plan usage (roughly 3-5 times a normal turn)."
GEMINI_PREFERRED = ("gemini-3.1-flash-image", "gemini-nano-banana-2.1", "gemini-3-pro-image", "gemini-3.1-flash-lite-image")

STYLES = {
    "none": ("No preset", ""),
    "photo": ("Photo", "Photorealistic photograph, natural lighting, sharp focus, realistic textures, shot on a professional camera."),
    "product": ("Product", "Clean professional product photography on a seamless neutral studio background, soft diffused lighting, crisp detail, centered composition."),
    "illustration": ("Illustration", "Polished digital illustration with clean linework and a cohesive color palette."),
    "3d": ("3D render", "High-quality 3D render, physically based materials, soft global illumination, subtle depth of field."),
    "flat-icon": ("Flat icon", "Flat vector icon style, simple geometric shapes, bold solid colors, no gradients or shadows, centered on a plain background."),
    "anime": ("Anime", "Anime style illustration, expressive characters, clean cel shading, vibrant colors."),
    "watercolor": ("Watercolor", "Soft watercolor painting, visible paper texture, gentle color bleeds and delicate brush strokes."),
    "cinematic": ("Cinematic", "Cinematic still, dramatic lighting, shallow depth of field, anamorphic widescreen look, rich color grading."),
    "logo": ("Logo", "Minimalist vector logo mark, simple memorable shape, balanced composition, solid colors, plain background, no mockup."),
}
ENGINE_PARAMS = {
    "codex": {"aspect_ratios": ["1:1", "3:2", "2:3", "4:3", "3:4", "16:9", "9:16"], "sizes": ["auto", "1K", "2K"],
              "qualities": list(QUALITIES), "formats": list(FORMATS), "max_count": MAX_COUNT, "max_references": 5},
    "gemini": {"aspect_ratios": list(ASPECTS), "sizes": ["512px", "1K", "2K", "4K"], "formats": ["png", "jpeg"],
               "max_count": MAX_COUNT, "max_references": 14},
    "gateway": {"aspect_ratios": ["auto", "1:1", "3:2", "2:3"], "sizes": ["auto", "1K"], "qualities": list(QUALITIES),
                "formats": list(FORMATS), "max_count": MAX_COUNT, "max_references": 0, "background": list(BACKGROUNDS)},
}
GATEWAY_LIKE = re.compile(r"image|dall-?e|imagen|flux|stable-diffusion|sdxl", re.IGNORECASE)
LIMIT_WORDS = ("usage limit", "rate limit", "quota exceeded", "limit reached")


class Unavailable(Exception):
    """An engine cannot serve this request; `str(exc)` says what to configure."""


class HttpFailure(Exception):
    def __init__(self, status, text):
        super().__init__(status)
        self.status, self.text = status, str(text)


def sniff(raw):
    """(extension, mime) for PNG/JPEG/WebP bytes, else None."""
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "png", "image/png"
    if raw[:3] == b"\xff\xd8\xff":
        return "jpg", "image/jpeg"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "webp", "image/webp"
    return None


def dimensions(raw):
    """(width, height) of PNG/JPEG/WebP bytes, or (None, None)."""
    try:
        kind = (sniff(raw) or ("", ""))[0]
        if kind == "png":
            return struct.unpack(">II", raw[16:24])
        if kind == "jpg":
            index = 2
            while index + 9 < len(raw):
                if raw[index] != 0xFF:
                    index += 1
                    continue
                marker = raw[index + 1]
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    index += 2
                    continue
                size = struct.unpack(">H", raw[index + 2:index + 4])[0]
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    height, width = struct.unpack(">HH", raw[index + 5:index + 9])
                    return width, height
                index += 2 + size
        if kind == "webp":
            chunk = raw[12:16]
            if chunk == b"VP8 ":
                return struct.unpack("<HH", raw[26:30])[0] & 0x3FFF, struct.unpack("<HH", raw[26:30])[1] & 0x3FFF
            if chunk == b"VP8L":
                bits = struct.unpack("<I", raw[21:25])[0]
                return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
            if chunk == b"VP8X":
                return int.from_bytes(raw[24:27], "little") + 1, int.from_bytes(raw[27:30], "little") + 1
    except (struct.error, IndexError):
        pass
    return None, None


def gemini_model_info(model):
    """Per-model limits (documented behaviour; see the report's unverified assumptions)."""
    name = str(model).casefold()
    if "flash-lite" in name or "2.5-flash-image" in name:
        sizes = ["1K"]
    elif "3.1-flash-image" in name:
        sizes = ["512px", "1K", "2K", "4K"]
    else:
        sizes = ["1K", "2K", "4K"]
    return {"sizes": sizes, "max_references": 3 if "2.5-flash-image" in name else 14, "legacy": "2.5-flash-image" in name}


def b64_images(value, out, paths, depth=0):
    """Collect decoded images (base64 / data URLs) and image-file paths from a parsed JSON event."""
    if depth > 14:
        return
    if isinstance(value, dict):
        for item in value.values():
            b64_images(item, out, paths, depth + 1)
    elif isinstance(value, list):
        for item in value:
            b64_images(item, out, paths, depth + 1)
    elif isinstance(value, str):
        text = value.strip()
        if text.startswith("data:image/") and "base64," in text[:80]:
            text = text.split("base64,", 1)[1]
        if len(text) >= 64 and re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", text):
            try:
                raw = base64.b64decode(text)
            except ValueError:
                return
            if sniff(raw):
                out.append(raw)
        elif len(text) < 3000:
            for pattern in (r"(?:[A-Za-z]:[\\/]|/)[^\s\"'<>|*?]+?\.(?:png|jpe?g|webp)\b",
                            r"[A-Za-z]:[\\/][^\"'<>|*?\n]+?\.(?:png|jpe?g|webp)\b"):
                paths.extend(m.group(0) for m in re.finditer(pattern, text, re.IGNORECASE))


class Ref:
    def __init__(self, path, mime, size):
        self.path, self.mime, self.size = Path(path), mime, size

    def b64(self):
        return base64.b64encode(self.path.read_bytes()).decode()


class Choice:
    def __init__(self, engine, model, notice="", via=""):
        self.engine, self.model, self.notice, self.via = engine, model, notice, via


class ImageEngines:
    def __init__(self, engine):
        self.engine = engine
        self.store, self.providers, self.vault = engine.store, engine.providers, engine.vault
        self.codex_lock = threading.Lock()
        self.catalog_cache = {}
        self.migrate()

    # ---- settings -------------------------------------------------------------------------------------
    def migrate(self):
        """One-time: the legacy `image_model` text setting becomes image.mode='fixed' on the gateway engine."""
        settings = self.store.settings()
        legacy = str(settings.get("image_model") or "").strip()
        if "image" not in settings and legacy:
            self.store.settings({"image": self.fill({"mode": "fixed", "engine": "gateway", "model": legacy})})

    @staticmethod
    def fill(raw):
        raw = raw if isinstance(raw, dict) else {}
        result = {"mode": raw.get("mode") if raw.get("mode") in ("agent", "fixed") else "agent"}
        if raw.get("engine") in ENGINE_IDS:
            result["engine"] = raw["engine"]
        if isinstance(raw.get("model"), str) and raw["model"].strip():
            result["model"] = raw["model"].strip()[:120]
        if result["mode"] == "fixed" and "engine" not in result:
            result["mode"] = "agent"
        result["fallback"] = [{"engine": e["engine"], "model": str(e.get("model") or "")[:120]}
                              for e in raw.get("fallback", []) if isinstance(e, dict) and e.get("engine") in ENGINE_IDS][:5]
        defaults = raw.get("defaults") if isinstance(raw.get("defaults"), dict) else {}
        result["defaults"] = {**DEFAULTS, **{k: v for k, v in defaults.items() if k in DEFAULTS}}
        return result

    def current(self):
        settings = self.store.settings()
        raw = settings.get("image")
        if not isinstance(raw, dict) and str(settings.get("image_model") or "").strip():
            raw = {"mode": "fixed", "engine": "gateway", "model": settings["image_model"]}
        return self.fill(raw)

    def validate(self, value, previous=None):
        """Normalise a (partial) `image` settings update against the current settings."""
        if not isinstance(value, dict) or set(value) - {"mode", "engine", "model", "fallback", "defaults"}:
            raise DomainError("Image settings accept only mode, engine, model, fallback and defaults.")
        merged = {**(previous if previous is not None else self.current())}
        merged["defaults"] = dict(merged.get("defaults") or DEFAULTS)
        for key in ("mode", "engine", "model"):
            if key in value:
                if value[key] in (None, ""):
                    merged.pop(key, None)
                else:
                    merged[key] = value[key]
        if merged.get("mode", "agent") not in ("agent", "fixed"):
            raise DomainError("Image mode must be agent or fixed.")
        merged["mode"] = merged.get("mode", "agent")
        if "engine" in merged and merged["engine"] not in ENGINE_IDS:
            raise DomainError("Choose an image engine: codex, gemini or gateway.")
        if "model" in merged and (not isinstance(merged["model"], str) or not re.fullmatch(r"[A-Za-z0-9._:/@+-]{1,120}", merged["model"].strip())):
            raise DomainError("Choose an image model from the list.")
        if merged["mode"] == "fixed" and "engine" not in merged:
            raise DomainError("Fixed mode needs an image engine.")
        if "fallback" in value:
            fallback = value["fallback"]
            if not isinstance(fallback, list) or len(fallback) > 5:
                raise DomainError("The image fallback list holds at most 5 engines.")
            merged["fallback"] = []
            for item in fallback:
                if not isinstance(item, dict) or item.get("engine") not in ENGINE_IDS or set(item) - {"engine", "model"}:
                    raise DomainError("Each fallback needs an engine (codex, gemini or gateway) and an optional model.")
                model = item.get("model") or ""
                if model and (not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9._:/@+-]{1,120}", model)):
                    raise DomainError("Invalid fallback model.")
                entry = {"engine": item["engine"], "model": model}
                if entry not in merged["fallback"]:
                    merged["fallback"].append(entry)
        if "defaults" in value:
            defaults = value["defaults"]
            if not isinstance(defaults, dict) or set(defaults) - set(DEFAULTS):
                raise DomainError("Image defaults accept aspect_ratio, size, quality, format, count and style.")
            checks = {"aspect_ratio": ASPECTS + ("auto",), "size": SIZES, "quality": QUALITIES, "format": FORMATS, "style": tuple(STYLES)}
            for key, item in defaults.items():
                if key == "count":
                    if isinstance(item, bool) or not isinstance(item, int) or not 1 <= item <= MAX_COUNT:
                        raise DomainError("The default image count must be a whole number from 1 to %d." % MAX_COUNT)
                elif item not in checks[key]:
                    raise DomainError("Invalid default %s: choose one of %s." % (key.replace("_", " "), ", ".join(checks[key])))
                merged["defaults"][key] = item
        return self.fill(merged)

    # ---- engine status --------------------------------------------------------------------------------
    def binding(self, agent=None, run=None):
        models = self.engine.models
        try:
            actor = agent or self.store.get("agents", "a-lead")
        except DomainError:
            return self.engine.default_binding()
        try:
            return models.effective(actor, run or {})
        except DomainError:
            return models.base(actor)

    def codex_signed_in(self):
        accounts = self.providers.accounts
        state = accounts.current.get("codex")
        if state:
            return bool(state.get("signed_in")) and "codex" not in self.providers.account_errors
        return bool(accounts.has_identity("codex"))

    def catalog(self, provider):
        return [m for m in self.providers.models if m.get("provider") == provider]

    def status(self, engine_id, hint="", explicit=False):
        """(models[{id,label}], reason) -- reason is '' when the engine is usable."""
        if engine_id == "codex":
            if not self.providers.cli.get("codex"):
                return [], "Codex CLI is not installed on the runtime machine."
            if not self.codex_signed_in():
                return [], "Sign in to Codex (Connections > Codex) to use its built-in image generation."
            models = [{"id": m["id"], "label": m.get("name") or m["id"]} for m in self.catalog("codex")]
            return models, ""
        if engine_id == "gemini":
            if not self.vault.get("GEMINI_API_KEY"):
                return [], "Add a Gemini API key (Connections > Gemini). Signing in with Google alone cannot generate images."
            try:
                models = self.gemini_models()
            except DomainError as exc:
                return [], str(exc)
            if not models:
                return [], "The Gemini API lists no image models for this key."
            return [{"id": m, "label": m} for m in models], ""
        gateway = str(self.store.settings().get("gateway_url") or "").strip()
        found = [m["id"] for m in self.catalog("gateway") if GATEWAY_LIKE.search(str(m.get("id")))]
        chosen = hint or ""
        current = self.current()
        if current.get("engine") == "gateway" and current.get("model"):
            chosen = chosen or current["model"]
        for item in [chosen] + [e["model"] for e in current["fallback"] if e["engine"] == "gateway"]:
            if item and item not in found:
                found.append(item)
        if not found:
            return [], "Choose an image model for the gateway in Settings > Image generation."
        if not gateway and not explicit:
            # An explicitly chosen model (fixed/requested) is let through: the gateway call reports the missing URL.
            return [], "Set a gateway URL (Connections > Gateway) that serves /images/generations."
        return [{"id": m, "label": m} for m in found], ""

    def describe(self, engine_id, agent=None):
        current = self.current()
        hint = current.get("model") if current.get("engine") == engine_id else ""
        models, reason = self.status(engine_id, hint or "")
        entry = {"id": engine_id, "label": LABELS[engine_id], "available": not reason, "models": [],
                 "params": json.loads(json.dumps(ENGINE_PARAMS[engine_id]))}
        if reason:
            entry["reason"] = reason
        if engine_id == "codex":
            entry["notice"] = CODEX_NOTICE
        try:
            default = self.default_model(engine_id, models, hint or "", self.binding(agent))
        except Unavailable:
            default = ""
        ids = [m["id"] for m in models]
        if default and default not in ids:
            models = [{"id": default, "label": default}] + models
        for model in models:
            row = {"id": model["id"], "label": model["label"], **({"default": True} if model["id"] == default else {})}
            if engine_id == "gemini":
                info = gemini_model_info(model["id"])
                row.update(sizes=info["sizes"], max_references=info["max_references"])
            entry["models"].append(row)
        return entry

    def listing(self, agent=None):
        return {"engines": [self.describe(e, agent) for e in ENGINE_IDS], "settings": self.current(),
                "styles": [{"id": k, "label": v[0]} for k, v in STYLES.items()]}

    def default_model(self, engine_id, models, hint, binding):
        ids = [m["id"] for m in models]
        if engine_id == "codex":
            if binding.get("provider") == "codex" and binding.get("model"):
                return binding["model"]
            settings = self.store.settings()
            if settings.get("default_provider") == "codex" and settings.get("default_model"):
                return settings["default_model"]
            if hint:
                return hint
            if ids:
                return ids[0]
            raise Unavailable("No Codex model is available yet. Open Connections and refresh the Codex models.")
        if hint and (not ids or hint in ids):
            return hint
        for preferred in GEMINI_PREFERRED if engine_id == "gemini" else ():
            if preferred in ids:
                return preferred
        if ids:
            return ids[0]
        raise Unavailable("No image model is available for this engine.")

    # ---- Gemini catalog -------------------------------------------------------------------------------
    def gemini_models(self):
        shared = [str(m["id"]).removeprefix("models/") for m in self.catalog("gemini") if "image" in (m.get("capabilities") or [])]
        if shared:
            return shared
        key = self.vault.get("GEMINI_API_KEY")
        digest = hashlib.sha256(key.encode()).hexdigest()
        cached = self.catalog_cache.get("gemini")
        if cached and cached["key"] == digest and time.monotonic() < cached["until"]:
            if cached["error"]:
                raise DomainError(cached["error"], 502)
            return cached["models"]
        try:
            models = self.fetch_gemini_models(key)
        except DomainError as exc:
            self.catalog_cache["gemini"] = {"key": digest, "until": time.monotonic() + 60, "models": [], "error": str(exc)}
            raise
        self.catalog_cache["gemini"] = {"key": digest, "until": time.monotonic() + CATALOG_TTL, "models": models, "error": ""}
        return models

    def fetch_gemini_models(self, key):
        names, token = [], ""
        for _ in range(5):
            url = GEMINI_BASE + "/models?pageSize=1000" + ("&pageToken=" + token if token else "")
            try:
                page = self.request(url, None, {"x-goog-api-key": key}, timeout=20)
            except HttpFailure as exc:
                raise DomainError(self.gemini_error(exc, "read the Gemini model list"), 401 if exc.status in (401, 403) else 502) from None
            names += [str(m.get("name", "")).removeprefix("models/") for m in page.get("models", []) if isinstance(m, dict)]
            token = page.get("nextPageToken") or ""
            if not token:
                break
        return sorted({n for n in names if "image" in n.casefold() and not n.casefold().startswith(("imagen", "veo")) and "embedding" not in n.casefold()})

    # ---- resolution -----------------------------------------------------------------------------------
    def prepare(self, engine_id, model, binding, refs):
        """A Choice for the engine, or Unavailable explaining why not."""
        current = self.current()
        hint = current.get("model") if current.get("engine") == engine_id else ""
        models, reason = self.status(engine_id, model or hint or "", explicit=bool(model))
        if reason:
            raise Unavailable(reason)
        ids = [m["id"] for m in models]
        if model:
            if engine_id != "codex" and ids and model not in ids:
                raise Unavailable("The model " + model + " is not in the " + LABELS[engine_id] + " list.")
            if engine_id == "codex" and ids and model not in ids and model != binding.get("model"):
                raise Unavailable("The model " + model + " is not in the Codex catalog.")
            chosen = model
        else:
            chosen = self.default_model(engine_id, models, hint or "", binding)
        limit = ENGINE_PARAMS[engine_id]["max_references"]
        if engine_id == "gemini":
            limit = gemini_model_info(chosen)["max_references"]
        if refs > limit:
            raise Unavailable(LABELS[engine_id] + (" does not accept reference images." if not limit else " accepts at most %d reference images." % limit))
        return Choice(engine_id, chosen, CODEX_NOTICE if engine_id == "codex" else "")

    def resolve(self, agent=None, run=None, request=None, source="owner", refs=0):
        current, request = self.current(), request or {}
        binding = self.binding(agent, run)
        honour = source == "owner" or current["mode"] == "agent"
        asked_engine = request.get("engine") if request.get("engine") in ENGINE_IDS else None
        if honour and request.get("engine") not in (None, "") and not asked_engine:
            raise DomainError("Choose an image engine: codex, gemini or gateway.")
        if honour and not asked_engine and request.get("model"):
            # A model without an engine selects the engine that lists it.
            asked_engine = next((e for e in ENGINE_IDS if str(request["model"]) in [m["id"] for m in self.status(e)[0]]), None)
            if not asked_engine:
                raise DomainError("Unknown image model " + str(request["model"])[:80] + ". Choose one from the image engine list.")
        strict, candidates, primary = False, [], ""
        if asked_engine and honour:
            strict, candidates = True, [(asked_engine, str(request.get("model") or ""))]
        elif current["mode"] == "fixed":
            strict, candidates = True, [(current["engine"], current.get("model", ""))]
        else:
            provider = binding.get("provider")
            if provider == "codex":
                primary = "codex"
                candidates.append(("codex", binding.get("model", "")))
            elif provider == "gemini":
                primary = "gemini"
                candidates.append(("gemini", current.get("model", "") if current.get("engine") == "gemini" else ""))
            candidates += [(e["engine"], e["model"]) for e in current["fallback"]]
            candidates += [(e, "") for e in ENGINE_IDS]
        reasons, seen = {}, set()
        for engine_id, model in candidates:
            if (engine_id, model) in seen:
                continue
            seen.add((engine_id, model))
            try:
                choice = self.prepare(engine_id, model, binding, refs)
            except Unavailable as exc:
                if strict:
                    raise DomainError(LABELS[engine_id] + " is not available: " + str(exc), 409) from None
                reasons.setdefault(engine_id, str(exc))
                continue
            choice.via = "requested" if asked_engine and honour else "fixed" if strict else "agent" if engine_id == primary else "fallback"
            if primary and engine_id != primary and primary in reasons:
                choice.notice = (choice.notice + " " if choice.notice else "") + "The agent's own provider cannot generate images (" + reasons[primary].rstrip(".") + "); used " + LABELS[engine_id] + " instead."
            return choice
        raise DomainError("No image engine is available. " + " ".join(LABELS[e] + ": " + reasons.get(e, "not tried.") for e in ENGINE_IDS)
                          + " Configure one in Settings > Image generation.", 409)

    # ---- parameters and prompt ------------------------------------------------------------------------
    def params(self, choice, data):
        defaults = self.current()["defaults"]
        spec = ENGINE_PARAMS[choice.engine]
        sizes = gemini_model_info(choice.model)["sizes"] if choice.engine == "gemini" else spec["sizes"]
        allowed = {"aspect_ratio": spec["aspect_ratios"], "size": sizes, "quality": spec.get("qualities"), "format": spec["formats"]}
        result = {}
        for key, options in allowed.items():
            if options is None:
                continue
            explicit = data.get(key) not in (None, "")
            value = data[key] if explicit else defaults[key]
            if value not in options:
                if explicit:
                    raise DomainError("%s does not support %s %s. Choose one of: %s." % (LABELS[choice.engine], key.replace("_", " "), value, ", ".join(options)))
                value = {"aspect_ratio": "1:1", "size": "1K", "quality": "auto", "format": "png"}[key]
                if value not in options:
                    value = options[0]
            result[key] = value
        count = data.get("count") if data.get("count") not in (None, "") else defaults["count"]
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= spec["max_count"]:
            raise DomainError("Choose 1 to %d images." % spec["max_count"])
        result["count"] = count
        style = data.get("style") if data.get("style") not in (None, "") else defaults["style"]
        if style not in STYLES:
            raise DomainError("Unknown style. Choose one of: " + ", ".join(STYLES))
        result["style"] = style
        if choice.engine == "gateway":
            background = data.get("background")
            if background not in (None, ""):
                if background not in BACKGROUNDS:
                    raise DomainError("Background must be auto, transparent or opaque.")
                result["background"] = background
        return result

    @staticmethod
    def compose(prompt, style, negative):
        text = prompt
        if STYLES[style][1]:
            text += "\n\nStyle: " + STYLES[style][1]
        if negative:
            text += "\n\nAvoid: " + negative + "."
        return text

    # ---- references -----------------------------------------------------------------------------------
    def references(self, ids, run, tool):
        if ids in (None, ""):
            return []
        if not isinstance(ids, list) or len(ids) > 14 or any(not isinstance(i, str) for i in ids):
            raise DomainError("reference_artifact_ids must list at most 14 image IDs.")
        result = []
        for ref_id in dict.fromkeys(ids):
            path = None
            try:
                item, path = self.engine.outputs.resolve("artifact", ref_id)
                if not item.get("is_image"):
                    raise DomainError("Reference " + ref_id + " is not an image.")
                if tool and item.get("project_id") != run.get("project_id"):
                    raise DomainError("Reference " + ref_id + " belongs to another project.", 403)
            except DomainError as exc:
                if exc.status != 404:
                    raise
                try:
                    item = self.store.get("attachments", ref_id)
                except DomainError:
                    raise DomainError("Reference image " + ref_id + " was not found.", 404) from None
                if not item.get("is_image") or tool and ref_id not in (run.get("attachment_ids") or []):
                    raise DomainError("Reference " + ref_id + " is not an image available to this run.", 403)
                path = self.engine.attachments.path(item)
            with path.open("rb") as handle:
                head = handle.read(16)
            kind = sniff(head + b"\0" * 16)
            if not kind:
                raise DomainError("Reference " + ref_id + " is not a PNG, JPEG or WebP image.")
            result.append(Ref(path, kind[1], path.stat().st_size))
        return result

    # ---- HTTP -----------------------------------------------------------------------------------------
    def request(self, url, body=None, headers=None, timeout=120):
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **(headers or {})},
                                     method="POST" if data is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                raw = response.read(80 * 1024 * 1024)
        except urllib.error.HTTPError as exc:
            try:
                text = exc.read().decode("utf-8", "replace")[:4000]
            except (OSError, ValueError):
                text = ""
            raise HttpFailure(exc.code, text) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise DomainError("Cannot reach the Gemini API. Check the network connection and try again.", 502) from None
        try:
            return json.loads(raw)
        except ValueError:
            raise DomainError("The Gemini API returned an unreadable response.", 502) from None

    def gemini_error(self, exc, action):
        message = ""
        try:
            message = str(json.loads(exc.text).get("error", {}).get("message", ""))
        except (ValueError, AttributeError):
            pass
        message = self.vault.redact(message)[:300]
        if exc.status in (401, 403):
            return "The Gemini API key was rejected (HTTP %d). Check the key in Connections > Gemini." % exc.status
        if exc.status == 429:
            return "The Gemini API quota or rate limit was reached. Try again later or choose another image engine."
        return "Gemini could not %s (HTTP %d)%s" % (action, exc.status, ": " + message if message else ".")

    # ---- engines --------------------------------------------------------------------------------------
    def run_gemini(self, choice, text, params, refs):
        key = self.vault.get("GEMINI_API_KEY")
        info = gemini_model_info(choice.model)
        headers = {"x-goog-api-key": key}
        for ref in refs:
            if ref.size > GEMINI_REF_CAP:
                raise DomainError("Reference images for Gemini are limited to 7 MB each.")
        images, warnings, interactions = [], [], True
        mime = "image/jpeg" if params["format"] == "jpeg" else "image/png"
        encoded = [(ref.mime, ref.b64()) for ref in refs]
        for index in range(params["count"]):
            try:
                found = None
                if interactions:
                    config = {"type": "image", "mime_type": mime, "aspect_ratio": params["aspect_ratio"]}
                    if not info["legacy"]:
                        config["image_size"] = params["size"]
                    body = {"model": choice.model, "response_format": config,
                            "input": text if not encoded else [{"type": "text", "text": text}] +
                            [{"type": "image", "mime_type": m, "data": d} for m, d in encoded]}
                    try:
                        found = self.request(GEMINI_BASE + "/interactions", body, headers)
                    except HttpFailure as exc:
                        lowered = exc.text.casefold()
                        if exc.status == 404 or exc.status == 400 and ("unknown" in lowered or "invalid json payload" in lowered):
                            interactions = False
                        else:
                            raise
                if not interactions:
                    image_config = {"aspectRatio": params["aspect_ratio"], **({} if info["legacy"] else {"imageSize": params["size"]})}
                    body = {"contents": [{"role": "user", "parts": [{"text": text}] + [{"inlineData": {"mimeType": m, "data": d}} for m, d in encoded]}],
                            "generationConfig": {"responseModalities": ["IMAGE"], "imageConfig": image_config}}
                    found = self.request(GEMINI_BASE + "/models/" + choice.model + ":generateContent", body, headers)
                raws = []
                self.gemini_images(found, raws)
                if not raws:
                    raise DomainError(self.gemini_refusal(found), 502)
                images.append(raws[0])
            except HttpFailure as exc:
                error = DomainError(self.gemini_error(exc, "generate the image"), 429 if exc.status == 429 else 401 if exc.status in (401, 403) else 502)
                if not images:
                    raise error from None
                warnings.append(str(error))
                break
            except DomainError as exc:
                if not images:
                    raise
                warnings.append(str(exc))
                break
        return images, warnings

    @staticmethod
    def gemini_images(value, out, depth=0):
        """Base64 image payloads from an Interactions or generateContent response."""
        if depth > 12:
            return
        if isinstance(value, dict):
            data = value.get("data") if isinstance(value.get("data"), str) else None
            inline = value.get("inlineData") or value.get("inline_data")
            if isinstance(inline, dict) and isinstance(inline.get("data"), str):
                data = inline["data"]
            if data and (value.get("type") == "image" or str(value.get("mime_type") or value.get("mimeType") or "").startswith("image/")
                         or isinstance(inline, dict)):
                try:
                    raw = base64.b64decode(data)
                except ValueError:
                    raw = b""
                if sniff(raw):
                    out.append(raw)
                    return
            for item in value.values():
                ImageEngines.gemini_images(item, out, depth + 1)
        elif isinstance(value, list):
            for item in value:
                ImageEngines.gemini_images(item, out, depth + 1)

    @staticmethod
    def gemini_refusal(response):
        reason = ""
        if isinstance(response, dict):
            reason = str((response.get("promptFeedback") or {}).get("blockReason") or "")
            for candidate in response.get("candidates") or []:
                reason = reason or str(candidate.get("finishReason") or "")
        return "Gemini returned no image" + (" (" + reason.lower().replace("_", " ") + ")" if reason and reason != "STOP" else "") + ". Rephrase the prompt and try again."

    def run_gateway(self, choice, text, params):
        size = "auto"
        if params["aspect_ratio"] != "auto" and params["size"] != "auto":
            width, height = (int(p) for p in params["aspect_ratio"].split(":"))
            ratio = width / height
            size = "1536x1024" if ratio > 1.2 else "1024x1536" if ratio < 0.83 else "1024x1024"
        payload = {"model": choice.model, "prompt": text[:MAX_PROMPT], "n": params["count"], "size": size}
        if params.get("quality", "auto") != "auto":
            payload["quality"] = params["quality"]
        if params.get("background", "auto") != "auto":
            payload["background"] = params["background"]
        if params["format"] != "png":
            payload["output_format"] = params["format"]
        result = self.providers.gateway("/images/generations", payload)
        images, warnings = [], []
        for item in (result.get("data") if isinstance(result, dict) else None) or []:
            if not isinstance(item, dict):
                continue
            raw = None
            if item.get("b64_json"):
                try:
                    raw = base64.b64decode(item["b64_json"], validate=True)
                except ValueError:
                    warnings.append("The gateway returned an unreadable image.")
            elif item.get("url"):
                try:
                    raw = self.download(str(item["url"]))
                except DomainError as exc:
                    warnings.append(str(exc))
            if raw is not None:
                if len(raw) > MAX_IMAGE:
                    warnings.append("An image exceeded 15 MB and was skipped.")
                elif not sniff(raw):
                    warnings.append("The gateway returned a file that is not a PNG, JPEG or WebP image.")
                else:
                    images.append(raw)
        if not images:
            raise DomainError(warnings[0] if warnings else "The provider did not return a valid image.", 502)
        return images[:params["count"]], warnings

    def download(self, url):
        """Fetch an https image URL (public addresses only, <=15 MB)."""
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None, 443):
            raise DomainError("The gateway returned an image URL that is not public HTTPS.")
        try:
            addresses = sorted({info[4][0].split("%")[0] for info in socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)})
            for address in addresses:
                ip = ipaddress.ip_address(address)
                ip = getattr(ip, "ipv4_mapped", None) or ip
                if not ip.is_global:
                    raise DomainError("The gateway returned an image URL on a private address.")
            conn = PinnedConnection(parsed.hostname, addresses[0], 443)
            try:
                conn.request("GET", (parsed.path or "/") + ("?" + parsed.query if parsed.query else ""), headers={"User-Agent": "AgentFlow-Images/1.0"})
                response = conn.getresponse()
                if response.status != 200:
                    raise DomainError("The image URL returned HTTP %d." % response.status, 502)
                raw = response.read(MAX_IMAGE + 1)
            finally:
                conn.close()
        except DomainError:
            raise
        except (OSError, ValueError, IndexError):
            raise DomainError("Could not download the image from the gateway URL.", 502) from None
        if len(raw) > MAX_IMAGE:
            raise DomainError("The gateway image exceeds 15 MB.")
        return raw

    @staticmethod
    def codex_prompt(text, params, refs):
        count = params["count"]
        lines = ["$imagegen",
                 "Generate " + ("1 image" if count == 1 else "exactly %d separate images (one image generation per image, each a distinct variation)" % count)
                 + " with the built-in image generation tool. Do not ask questions, do not run commands, and do not write explanations.",
                 "Image description: " + text,
                 "Aspect ratio: " + params["aspect_ratio"] + "."]
        if params.get("size", "auto") != "auto":
            lines.append("Resolution: about %s on the long side." % {"1K": "1024 px", "2K": "2048 px"}.get(params["size"], params["size"]))
        if params.get("quality", "auto") != "auto":
            lines.append("Quality: " + params["quality"] + ".")
        lines.append("Output format: " + params["format"].upper() + ".")
        if refs:
            lines.append("%d reference image(s) are attached; use them as visual references for subject, style and composition." % len(refs))
        lines.append('When finished reply with only "done".')
        return "\n".join(lines)

    def run_codex(self, choice, text, params, refs, run):
        providers = self.providers
        executable = providers.cli.get("codex")
        if not executable:
            raise DomainError("Codex CLI not found. Install it and sign in on the runtime machine.")
        session = providers.accounts.check("codex")
        if not session.get("signed_in"):
            raise AccountUnavailable("codex")
        home = providers.accounts.home("codex")
        generated = home / "generated_images"
        scratch = self.store.directory / "image-scratch" / uid("img")
        scratch.mkdir(parents=True, exist_ok=True)
        args = [executable, "exec"]
        for ref in refs:
            args += ["--image", str(ref.path)]
        args += providers.codex_auth_args()
        args += ["--skip-git-repo-check", "--ephemeral", "--sandbox", "read-only", "--ignore-user-config",
                 "--config", 'web_search="disabled"', "--disable", "shell_tool", "--disable", "unified_exec",
                 "--disable", "apps", "--disable", "plugins", "--disable", "browser_use", "--disable", "computer_use",
                 "--disable", "multi_agent", "--disable", "code_mode_host",
                 "--enable", "image_generation", "--json", "--model", choice.model, "-"]
        key = (run or {}).get("id") or uid("image")
        try:
            with self.codex_lock:
                before = self.snapshot(generated)
                started = time.time()
                proc = subprocess.Popen(args, cwd=scratch, env=providers.child_environment(), stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                        errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                providers.processes[key] = proc
                try:
                    stdout, stderr = proc.communicate(self.codex_prompt(text, params, refs), timeout=TIMEOUT)
                except subprocess.TimeoutExpired:
                    providers.cancel(key)
                    proc.communicate()
                    raise DomainError("Codex image generation exceeded the 5-minute timeout.", 504) from None
                finally:
                    providers.processes.pop(key, None)
                images = self.codex_collect(stdout, generated, before, started, scratch, home)
            events = []
            for line in stdout.splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict):
                    events.append(event)
            for event in events:
                detail = json.dumps(event).casefold()
                if event.get("type") in ("error", "turn.failed") and any(w in detail for w in LIMIT_WORDS):
                    providers.accounts.assert_current("codex", session["fingerprint"])
                    raise ProviderLimit("codex", detail)
            if not images:
                if proc.returncode and any(w in stderr.casefold() for w in LIMIT_WORDS):
                    raise ProviderLimit("codex", stderr)
                if proc.returncode:
                    lowered = stderr.casefold()
                    if "image_generation" in lowered and ("unknown" in lowered or "unrecognized" in lowered):
                        raise DomainError("This Codex CLI version has no image_generation feature. Update Codex and try again.", 502)
                    providers.accounts.assert_current("codex", session["fingerprint"])
                    raise DomainError("Codex image generation failed (exit code %d). Check sign-in, quota and the CLI version." % proc.returncode, 502)
                said = "\n".join(e["item"].get("text", "") for e in events if e.get("type") == "item.completed"
                                 and isinstance(e.get("item"), dict) and e["item"].get("type") == "agent_message")
                raise DomainError("Codex finished but produced no image" + (": " + self.vault.redact(said)[:300] if said.strip() else ".")
                                  + " Make sure image generation is enabled for this Codex account.", 502)
            return images[:params["count"]], []
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    @staticmethod
    def snapshot(root):
        seen = {}
        if root.is_dir():
            for path in root.rglob("*"):
                try:
                    if path.suffix.lower() in IMAGE_EXT and path.is_file():
                        info = path.stat()
                        seen[str(path)] = (info.st_mtime_ns, info.st_size)
                except OSError:
                    continue
        return seen

    def codex_collect(self, stdout, generated, before, started, scratch, home):
        """New files under generated_images (+ scratch cwd), base64 payloads and image paths from the JSON events."""
        found, files = [], []
        for path, signature in self.snapshot(generated).items():
            if before.get(path) != signature and signature[0] / 1e9 >= started - 2:
                files.append(Path(path))
        files += [Path(p) for p in self.snapshot(scratch)]
        roots = [generated.resolve(), scratch.resolve()]
        embedded, paths = [], []
        for line in stdout.splitlines():
            try:
                b64_images(json.loads(line), embedded, paths)
            except ValueError:
                continue
        for text in paths:
            try:
                candidate = Path(text).resolve()
            except OSError:
                continue
            if candidate.is_file() and any(candidate.is_relative_to(root) for root in roots):
                files.append(candidate)
        raws = []
        for path in sorted(dict.fromkeys(files), key=lambda p: p.stat().st_mtime_ns if p.exists() else 0):
            try:
                if path.stat().st_size <= MAX_FILE:
                    raws.append(path.read_bytes())
            except OSError:
                continue
        raws += embedded
        for raw in raws:
            if sniff(raw) and len(raw) <= MAX_FILE and raw not in found:
                found.append(raw)
        return found

    # ---- generation -----------------------------------------------------------------------------------
    def generate(self, data, actor=None, run=None, source="owner"):
        if not isinstance(data, dict):
            raise DomainError("Invalid image request.")
        prompt = str(data.get("prompt") or "").strip()
        if not prompt:
            raise DomainError("Enter a description of the image.")
        if len(prompt) > MAX_PROMPT:
            raise DomainError("The image description is limited to 10,000 characters.")
        negative = str(data.get("negative") or "").strip()
        if len(negative) > 1000:
            raise DomainError("The negative prompt is limited to 1,000 characters.")
        run = run or {}
        tool = source == "agent"
        agent = actor
        if agent is None and data.get("agent_id"):
            agent = self.store.get("agents", str(data["agent_id"]))
        project_id = run.get("project_id") or data.get("project_id") or None
        if project_id:
            self.store.get("projects", project_id)
        refs = self.references(data.get("reference_artifact_ids"), run, tool)
        choice = self.resolve(agent, run, data, source, len(refs))
        params = self.params(choice, data)
        text = self.compose(prompt, params["style"], negative)
        if choice.engine == "codex":
            raws, warnings = self.run_codex(choice, text, params, refs, run)
        elif choice.engine == "gemini":
            raws, warnings = self.run_gemini(choice, text, params, refs)
        else:
            raws, warnings = self.run_gateway(choice, text, params)
        images = self.save(raws, choice, params, prompt, agent, run, project_id, data.get("title"))
        first = images[0]
        result = {"images": images, "engine": choice.engine, "model": choice.model, "params": params,
                  "path": first["path"], "url": first["url"], "artifact_id": first["artifact_id"]}
        notice = " ".join(x for x in [choice.notice, *warnings] if x)
        if notice:
            result["notice"] = notice
        if len(images) < params["count"]:
            result["partial"] = "%d of %d images were generated." % (len(images), params["count"])
        return result

    def save(self, raws, choice, params, prompt, agent, run, project_id, title):
        media = self.engine.workspace / "media"
        media.mkdir(parents=True, exist_ok=True)
        owner = (agent or {}).get("id") or "a-lead"
        result = []
        for raw in raws:
            kind = sniff(raw)
            path = media / (uid("image") + "." + kind[0])
            path.write_bytes(raw)
            width, height = dimensions(raw)
            artifact = self.store.create("artifacts", {
                "title": str(title or prompt)[:250].replace("\n", " "), "path": path.relative_to(self.engine.workspace).as_posix(),
                "absolute_path": str(path), "project_id": project_id, "agent_id": owner, "run_id": run.get("id"),
                "bytes": len(raw), "type": kind[0], "source": "workspace", "media_url": "/media/" + path.name, "is_image": True,
                "sha256": hashlib.sha256(raw).hexdigest(), "image": {"engine": choice.engine, "model": choice.model, "style": params["style"],
                "aspect_ratio": params["aspect_ratio"], "prompt": prompt[:2000]}})
            result.append({"artifact_id": artifact["id"], "path": artifact["path"], "url": "/media/" + path.name,
                           **({"width": width, "height": height} if width else {})})
        return result

    def tool(self, args, actor, run):
        """Agent tool image.generate (engine/model are honoured only while settings mode is 'agent')."""
        result = self.generate(args, actor=actor, run=run, source="agent")
        result["files"] = [i["path"] for i in result["images"]]
        return result
