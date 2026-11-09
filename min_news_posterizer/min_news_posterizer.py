import json
import logging
import os
import random
import re
import tempfile
import time
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path

import requests
from flask import abort, jsonify, request
from PIL import Image, ImageColor, ImageOps
from dotenv import load_dotenv, find_dotenv

from blueprints.plugin import plugin_bp
from plugins.base_plugin.base_plugin import BasePlugin
from utils.image_utils import pad_image_blur

from .constants import NEWSPAPERS
from .poster_rules import POSTER_RULES

logger = logging.getLogger(__name__)

DOTENV_PATH = find_dotenv(filename=".env", usecwd=False)
if DOTENV_PATH:
    load_dotenv(dotenv_path=DOTENV_PATH, override=False)


FREEDOM_FORUM_URL = "https://cdn.freedomforum.org/dfp/jpg{}/lg/{}.jpg"
ONE_MIN_CHAT_URL = "https://api.1min.ai/api/chat-with-ai"
ONE_MIN_FEATURES_URL = "https://api.1min.ai/api/features"
ONE_MIN_ASSET_BASE_URL = "https://asset.1min.ai"
PLUGIN_ID = "min_news_posterizer"
MAX_IMAGE_PROMPT_CHARS = 4000

VIBES_FILE = Path(__file__).parent / "vibes.json"

# Model registry - Image = 1min.ai, Front Page Analysis = 1min.ai
MODEL_CATALOG = {
    "image": {
        "gpt-image-2.5-sunburst":           {"label": "GPT Image 2.5 Sunburst (1min.ai)",         "provider": "1min"},
        "gpt-image-2.5-flare":              {"label": "GPT Image 2.5 Flare (1min.ai)",            "provider": "1min"},
        "gemini-3.1-flash-image-preview":   {"label": "Gemini 3.1 Flash Image Preview (1min.ai)", "provider": "1min"},
        "gemini-3-pro-image-preview":       {"label": "Gemini 3 Pro Image Preview (1min.ai)",     "provider": "1min"},
    },
    "analysis": {
        "gpt-6-luna":  {"label": "GPT-6 Luna (1min.ai)",  "provider": "1min"},
    },
}

DEFAULT_MODELS = {
    "image": "gpt-image-2.5-flare",
    # default headline analysis to GPT-6 Luna through 1min.ai.
    "analysis": os.getenv("VISION_MODEL", "gpt-6-luna").strip(),
}

MODEL_ALIASES = {
    "gemini-3-pro": "gemini-3-pro-image-preview",
    "gemini-3.1-flash": "gemini-3.1-flash-image-preview",
}

# Fixes Issue with the api key appearing blank and not rendering the drop-downs properly 
def _has_key(v) -> bool:
    return bool(v and str(v).strip())

def _load_1min_api_key(device_config=None) -> str:
    key_names = ("ONE_MIN_AI_API_KEY", "ONE_MIN_API_KEY", "ONEMIN_API_KEY", "1MIN_API_KEY")
    if device_config:
        for key_name in key_names:
            value = device_config.load_env_key(key_name)
            if _has_key(value):
                return value.strip()
    for key_name in key_names:
        value = os.getenv(key_name)
        if _has_key(value):
            return value.strip()
    return ""

def _one_min_headers(api_key: str) -> dict:
    return {
        "Content-Type": "application/json",
        "API-KEY": api_key,
    }

def _extract_1min_result_text(payload: dict) -> str:
    detail = (payload.get("aiRecord") or {}).get("aiRecordDetail") or {}
    result = detail.get("resultObject")
    if isinstance(result, list):
        return "\n".join(str(item) for item in result if item is not None).strip()
    if isinstance(result, str):
        return result.strip()
    return ""

def _extract_1min_image_url(payload: dict) -> str:
    record = payload.get("aiRecord") or {}
    temporary_url = (record.get("temporaryUrl") or "").strip()
    if temporary_url:
        return temporary_url
    detail = record.get("aiRecordDetail") or {}
    result = detail.get("resultObject")
    if isinstance(result, list) and result:
        first = str(result[0] or "").strip()
        if first.startswith(("http://", "https://")):
            return first
        if first:
            return f"{ONE_MIN_ASSET_BASE_URL}/{first.lstrip('/')}"
    elif isinstance(result, str) and result.strip():
        first = result.strip()
        if first.startswith(("http://", "https://")):
            return first
        if first:
            return f"{ONE_MIN_ASSET_BASE_URL}/{first.lstrip('/')}"
    return ""

def _raise_for_1min_status(resp: requests.Response, action: str) -> None:
    try:
        resp.raise_for_status()
    except requests.HTTPError as e:
        detail = ""
        try:
            body = resp.json()
            detail = json.dumps(body, ensure_ascii=False)
        except Exception:
            detail = (resp.text or "").strip()
        if detail:
            raise RuntimeError(f"{action}: {e}; response: {detail}") from e
        raise RuntimeError(f"{action}: {e}") from e

# Choose the user-selected image/analysis model from settings
def _pick_model(settings: dict, kind: str):
    key = "imageModel" if kind == "image" else "analysisModel"
    raw_model_id = (settings.get(key) or DEFAULT_MODELS.get(kind, "") or "").strip()
    model_id = MODEL_ALIASES.get(raw_model_id, raw_model_id)

    if model_id not in MODEL_CATALOG[kind]:
        logger.warning(f"Unknown {kind} model '{raw_model_id}', defaulting to {DEFAULT_MODELS.get(kind)}")
        model_id = (DEFAULT_MODELS.get(kind) or "").strip()

    if model_id not in MODEL_CATALOG[kind]:
        model_id = next(iter(MODEL_CATALOG[kind].keys()))
        logger.warning(f"{kind} default also invalid; falling back to first catalog entry: {model_id}")

    return model_id, MODEL_CATALOG[kind][model_id]

def _setting_enabled(settings: dict, key: str) -> bool:
    value = settings.get(key)
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}

# Load some vibes and get their descriptions
def _read_vibes() -> list:
    try:
        if not VIBES_FILE.exists():
            # create empty file once
            _atomic_write_json(VIBES_FILE, [])
            return []
        data = json.loads(VIBES_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []

def _get_vibe_description(vibe_id: str) -> str:
    vibe_id = (vibe_id or "").strip()
    if not vibe_id:
        return ""
    for v in _read_vibes():   # <-- use the one reader
        if (v.get("id") or "").strip() == vibe_id:
            return (v.get("description") or "").strip()
    return ""

def _pick_random_vibe_id() -> str:
    vibes = [
        v for v in _read_vibes()
        if isinstance(v, dict) and (v.get("id") or "").strip() and (v.get("description") or "").strip()
    ]
    if not vibes:
        return ""
    return random.choice(vibes).get("id", "").strip()

# Write into the vibes.json using atomic write
def _atomic_write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, path)  # atomic swap
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

# Vibe list house-keeping
def _slugify(label: str) -> str:
    s = (label or "").strip().lower()
    s = re.sub(r"[\"']", "", s)
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = re.sub(r"^_+|_+$", "", s)
    return s or "vibe"

def _sorted(vibes: list) -> list:
    return sorted(
        [v for v in vibes if isinstance(v, dict) and (v.get("id") or "").strip()],
        key=lambda v: str(v.get("label") or v.get("id") or "").casefold()
    )

# Define a GET endpoint and read me some vibes
@plugin_bp.get("/plugin/<plugin_id>/vibes/list")
def vibes_list(plugin_id):
    if plugin_id != PLUGIN_ID:
        abort(404)
    vibes = _sorted(_read_vibes())
    resp = jsonify({"ok": True, "vibes": vibes})
    # discourage caching so UI always reflects disk
    resp.headers["Cache-Control"] = "no-store"
    return resp

# Add vibe
@plugin_bp.post("/plugin/<plugin_id>/vibes/add")
def vibes_add(plugin_id):
    if plugin_id != PLUGIN_ID:
        abort(404)

    payload = request.get_json(silent=True) or {}
    label = (payload.get("label") or "").strip()
    description = (payload.get("description") or "").strip()

    if not label or not description:
        return jsonify({"ok": False, "error": "label_and_description_required"}), 400

    vibes = _read_vibes()

    # Reject duplicate LABEL (case-insensitive, trimmed)
    label_norm = re.sub(r"\s+", " ", label).strip().casefold()
    existing_labels = {
        re.sub(r"\s+", " ", (v.get("label") or "")).strip().casefold()
        for v in vibes
        if isinstance(v, dict)
    }
    if label_norm in existing_labels:
        return jsonify({
            "ok": False,
            "error": "duplicate_vibe_label",
            "message": "That vibe name already exists. Please pick another name."
        }), 409

    # You can still slugify into an ID (IDs can collide even if labels don't; rare, but safe)
    base_id = _slugify(label)
    existing_ids = {str(v.get("id") or "") for v in vibes if isinstance(v, dict)}
    if base_id in existing_ids:
        # If slug collides but label doesn't, require a different name to avoid confusion
        return jsonify({
            "ok": False,
            "error": "duplicate_vibe_id",
            "message": "That vibe name would create a duplicate ID. Please pick vibe name."
        }), 409

    vibe_id = base_id

    vibes.append({"id": vibe_id, "label": label, "description": description})
    vibes = _sorted(vibes)

    _atomic_write_json(VIBES_FILE, vibes)
    return jsonify({"ok": True, "vibes": vibes, "added_id": vibe_id})

# Delete a vibe - No confirmation, you better be sure! 
@plugin_bp.post("/plugin/<plugin_id>/vibes/delete")
def vibes_delete(plugin_id):
    if plugin_id != PLUGIN_ID:
        abort(404)

    payload = request.get_json(silent=True) or {}
    vibe_id = (payload.get("id") or "").strip()

    if not vibe_id:
        return jsonify({"ok": False, "error": "id_required"}), 400

    vibes = [v for v in _read_vibes() if isinstance(v, dict) and (v.get("id") or "").strip() != vibe_id]
    vibes = _sorted(vibes)

    _atomic_write_json(VIBES_FILE, vibes)
    return jsonify({"ok": True, "vibes": vibes, "deleted_id": vibe_id})

# Start the AI magic
class NewspaperPoster(BasePlugin):
    def __init__(self, config):
        super().__init__(config)
        self.config = config

    def generate_settings_template(self):
        template_params = super().generate_settings_template()

        template_params["api_key_1min"] = {
            "required": True,
            "service": "1min.ai",
            "expected_key": "ONE_MIN_AI_API_KEY",
        }

        # required by settings.html
        template_params["newspapers"] = NEWSPAPERS

        # ---- key presence (dotenv already loaded above) ----
        one_min_key = _load_1min_api_key()

        # ---- IMAGE models ----
        image_models = []
        for model_id, meta in MODEL_CATALOG["image"].items():
            provider = (meta or {}).get("provider")
            if provider == "1min" and not _has_key(one_min_key):
                continue
            image_models.append({"id": model_id, "label": meta.get("label", model_id)})
        template_params["image_models"] = image_models  # always present

        # ---- ANALYSIS models ----
        analysis_models = []
        for model_id, meta in MODEL_CATALOG["analysis"].items():
            provider = (meta or {}).get("provider")
            if provider == "1min" and not _has_key(one_min_key):
                continue
            analysis_models.append({"id": model_id, "label": meta.get("label", model_id)})
        template_params["analysis_models"] = analysis_models  # always present

        return template_params

    def get_analysis_client(self, device_config, model_meta):
        provider = (model_meta or {}).get("provider")
        if provider == "1min":
            api_key = _load_1min_api_key(device_config)
            return api_key or None

        return None
    

    # Grab that headline and article - or at least try by analyzing front page 

    def _looks_blocked_or_useless(self, parsed: dict, raw_text: str) -> bool:
        headline = (parsed.get("headline") or "").strip()
        subhead = (parsed.get("subhead") or "").strip()
        raw = (raw_text or "").strip()

        if not headline:
            return True

        refusal_markers = [
            "i'm sorry", "i'm unable", "i cannot assist", "i can’t assist",
            "i can't assist", "i can't help", "i cannot help",
            "can't help with that", "cannot help with that",
            "unable to comply", "copyright", "抱歉", "無法協助", "无法协助",
        ]

        low_text = f"{headline} {subhead}".lower()
        low_raw = raw.lower()

        if any(m in low_text for m in refusal_markers):
            return True
        if any(m in low_raw for m in refusal_markers) and len(raw) < 300:
            return True

        return False

    def analyze_front_page(self, image_url: str, model_id: str, model_meta: dict, device_config, traditional_chinese: bool = False):
        if traditional_chinese:
            language_rule = (
                "Write HEADLINE and SUBHEAD in natural Traditional Chinese as used in Taiwan (繁體中文). "
                "Use Traditional characters only, never Simplified. Use the usual Taiwanese Chinese name for "
                "people and places; if none exists, keep a short original name."
            )
            length_rule = (
                "- HEADLINE: at most 12 Chinese characters.\n"
                "- SUBHEAD: at most 20 Chinese characters.\n"
                "- Use common, everyday characters; avoid rare or archaic characters.\n"
            )
        else:
            language_rule = "Write HEADLINE and SUBHEAD in the same language as the newspaper."
            length_rule = (
                "- HEADLINE: at most 8 words.\n"
                "- SUBHEAD: at most 14 words.\n"
            )

        prompt_text = (
            "You are receiving a newspaper front page image. You must inspect the image itself.\n"
            "Do not say that you cannot view images. If the image is visible, perform OCR and visual reading.\n\n"
            "TASK: Find the single MAIN story (the biggest banner headline) and write the text for a news poster about it, "
            "so a reader understands the news at a glance.\n"
            f"LANGUAGE: {language_rule}\n\n"
            "OUTPUT FORMAT (exactly these 3 lines, nothing else):\n"
            "HEADLINE: <short, punchy poster headline>\n"
            "SUBHEAD: <one sentence saying who did what / what happened>\n"
            "SCENE: <one English sentence describing what to draw: the main person or actor, the place, and 1-3 key objects>\n\n"
            "RULES:\n"
            f"{length_rule}"
            "- SUBHEAD must add information and must NOT repeat the headline.\n"
            "- Use only facts from the front page; do not invent names, numbers, or places.\n"
            "- No newspaper name, dates, bylines, quotation marks, emoji, or hashtags.\n"
            "- SCENE must not ask for any text or lettering.\n"
            "- Do not explain your process or mention uncertainty.\n"
        )

        client = self.get_analysis_client(device_config, model_meta)
        if not client:
            logger.warning("No analysis client could be initialized.")
            return None

        provider = (model_meta or {}).get("provider")

        try:
            if provider == "1min":
                resp = requests.post(
                    ONE_MIN_CHAT_URL,
                    headers=_one_min_headers(client),
                    json={
                        "type": "UNIFY_CHAT_WITH_AI",
                        "model": model_id,
                        "promptObject": {
                            "prompt": prompt_text,
                            "attachments": {
                                "images": [image_url],
                            },
                        },
                    },
                    timeout=(10, 120),
                )
                _raise_for_1min_status(resp, "Front page analysis failed")
                text = _extract_1min_result_text(resp.json())

            else:
                logger.warning("Unknown analysis provider: %s", provider)
                return None

            if not text:
                logger.warning("Analysis returned empty text.")
                return None

            parsed = self._parse_poster_text(text)

            print("\n" + "=" * 60)
            print(f"DEBUG ANALYSIS RESULT ({provider.upper()})")
            print("HEADLINE:", parsed.get("headline"))
            print("SUBHEAD:", parsed.get("subhead"))
            print("SCENE:", parsed.get("scene"))
            print("=" * 60 + "\n")

            if self._looks_blocked_or_useless(parsed, text):
                logger.warning("Analysis blocked/refused or unusable for %s", image_url)
                return None

            return parsed

        except Exception as e:
            logger.exception("Image analysis failed: %s", e)
            return None


    # Parse HEADLINE / SUBHEAD / SCENE lines from the analysis output
    def _parse_poster_text(self, text: str) -> dict:
        fields = {"HEADLINE": "", "SUBHEAD": "", "SCENE": ""}
        for raw in (text or "").splitlines():
            line = raw.strip().lstrip("*#-• ").replace("**", "")
            match = re.match(r"^(HEADLINE|SUBHEAD|SCENE)\s*[:：]\s*(.*)$", line, re.IGNORECASE)
            if match and not fields[match.group(1).upper()]:
                # Quotes would break the quoted strings in the image prompt.
                value = re.sub(r"[\"“”「」『』]", "", match.group(2)).strip()
                fields[match.group(1).upper()] = value
        return {
            "headline": fields["HEADLINE"],
            "subhead": fields["SUBHEAD"],
            "scene": fields["SCENE"],
        }

    # Build prompt for the image model: vibe style + scene + exact poster text
    def build_ai_prompt(self, vibe_id: str, analysis: dict, traditional_chinese: bool = False) -> str:
        analysis = analysis or {}
        headline = (analysis.get("headline") or "").strip()
        subhead = (analysis.get("subhead") or "").strip()
        scene = (analysis.get("scene") or "").strip() or "A scene that illustrates the headline."

        if traditional_chinese:
            language = "Traditional Chinese (Taiwan, 繁體中文)"
            typeface = "a bold, clean Traditional Chinese typeface (黑體 style)"
        else:
            language = "the original language"
            typeface = "a bold, clean, highly legible typeface"

        text_lines = [f'1. Headline, large and dominant: "{headline}"']
        if subhead:
            text_lines.append(f'2. Subheadline, smaller, near the headline: "{subhead}"')
        count = "two pieces" if subhead else "one piece"

        body = (
            f"NEWS POSTER SCENE: {scene}\n\n"
            f"{str(POSTER_RULES).strip()}\n\n"
            "POSTER TEXT (overrides any text instructions in the style above):\n"
            f"The poster contains exactly {count} of text in {language}, copied character by character:\n"
            + "\n".join(text_lines) + "\n"
            "- Do not add, remove, change, or translate any character.\n"
            "- No other readable text anywhere, except short decorative stamp words explicitly named in the style above (e.g. APPROVED); callout boxes, signs, and labels stay blank.\n"
            f"- Use {typeface}; every character complete and correctly formed.\n"
            "- All text fully inside the poster with at least 8% margin from every edge; nothing cropped."
        )

        vibe_text = _get_vibe_description(vibe_id)
        # GPT Image 2.5 rejects prompts over 4000 chars; trim the vibe to fit.
        vibe_budget = MAX_IMAGE_PROMPT_CHARS - len(body) - 2
        if vibe_text and len(vibe_text) > vibe_budget:
            logger.warning("Vibe text trimmed from %d to %d chars to fit prompt limit", len(vibe_text), max(vibe_budget, 0))
            vibe_text = vibe_text[:max(vibe_budget, 0)].rstrip()

        final_prompt = (f"{vibe_text}\n\n{body}" if vibe_text else body).strip()

        #print("\n==== FINAL IMAGE PROMPT ====\n" + final_prompt + "\n==== END FINAL IMAGE PROMPT ====\n", flush=True)
        return final_prompt

   # Decide what to generate (find front page → analyze → build prompt)
    def generate_image(self, settings, device_config):
        """
        Coordinates the workflow: 
        1. Find newspaper -> 2. Analyze page -> 3. Build prompt -> 4. Generate art.
        """
        # 1. Resolve Settings
        newspaper_slug = settings.get("newspaper_id") or settings.get("newspaperSlug")
        if not newspaper_slug:
            raise RuntimeError("Newspaper ID not provided in settings.")

        newspaper_slug = newspaper_slug.upper()
        today = datetime.today()

        # 2. Date Cycling: Try Next Day, Today, then 2 Days Prior
        # This helps if today's paper isn't uploaded yet or is blocked by AI filters.
        days = [today + timedelta(days=diff) for diff in range(1, -8, -1)]


        analysis = None
        selected_url = None
        traditional_chinese = _setting_enabled(settings, "traditionalChinese")

        for date in days:
            image_url = FREEDOM_FORUM_URL.format(date.day, newspaper_slug)
            
            try:
                # Use a head request to see if the image exists without downloading it
                check = requests.head(image_url, timeout=(5, 15), allow_redirects=True)
                
                if check.status_code == 200:
                    logger.info(f"Found {newspaper_slug} for day {date.day}. Starting analysis...")
                    
                    analysis_model_id, analysis_meta = _pick_model(settings, "analysis")
                    
                    # Call analysis with the required device_config argument
                    analysis = self.analyze_front_page(
                        image_url,
                        analysis_model_id,
                        analysis_meta,
                        device_config,
                        traditional_chinese=traditional_chinese,
                    )

                    # If analysis returns None (due to refusal markers), the loop continues to the next day
                    if analysis:
                        selected_url = image_url
                        break
                else:
                    logger.info(f"Newspaper not available at {image_url} (Status: {check.status_code})")
            except Exception as e:
                logger.warning(f"Failed to check URL {image_url}: {e}")

        # If we exhausted all dates and found nothing or were blocked by every attempt
        if not analysis:
            raise RuntimeError(
                "Could not extract headline text from the front page. "
                "The AI may be blocking this specific paper, or the archive is unavailable."
            )

        # 3. Build Prompt and Generate Final Image
        random_vibe = _setting_enabled(settings, "randomVibe")
        vibe_id = _pick_random_vibe_id() if random_vibe else settings.get("vibe_id")
        ai_prompt = self.build_ai_prompt(vibe_id, analysis, traditional_chinese=traditional_chinese)
        
        return self.generate_1min_image(ai_prompt, settings, device_config)

    #Render (prompt -> 1min.ai image -> fit to screen).
    def generate_1min_image(self, ai_prompt: str, settings, device_config) -> Image.Image:
        """
        Communicates with 1min.ai to generate the poster art based on the analyzed headline.
        """
        # Load API Key via framework standard
        api_key = _load_1min_api_key(device_config)
        if not api_key:
            raise RuntimeError("1min.ai API key not found (ONE_MIN_AI_API_KEY).")

        # 1. Resolve Model and Display Dimensions
        image_model_id, _image_meta = _pick_model(settings, "image")
        orientation = (device_config.get_config("orientation") or "horizontal").lower()
        w, h = device_config.get_resolution()

        # Handle rotation/orientation for target dimensions
        if orientation == "vertical" and w > h:
            w, h = h, w
        elif orientation == "horizontal" and h > w:
            w, h = h, w

        # Map display orientation to the requested generation size.
        is_horizontal = (orientation == "horizontal")
        aspect_ratio = "3:2" if is_horizontal else "2:3"
        # 1024x640 is the smallest size GPT Image 2.5 allows (655,360 px); enough for e-ink.
        size = "1024x640" if is_horizontal else "640x1024"

        if image_model_id == "gemini-3.1-flash-image-preview":
            prompt_object = {
                "prompt": ai_prompt,
                "imageSize": "2K",
                "aspectRatio": aspect_ratio,
                "temperature": 1.0,
                "topP": 0.95,
            }
        elif image_model_id == "gemini-3-pro-image-preview":
            prompt_object = {
                "prompt": ai_prompt,
                "imageSize": "2K",
                "temperature": 1.0,
                "topP": 0.95,
            }
        else:
            # gpt-image-2.5-sunburst, gpt-image-2.5-flare
            prompt_object = {
                "prompt": ai_prompt,
                "n": 1,
                "size": size,
                "quality": "medium",
                "output_format": "png",
            }

        logger.info(f"Generating image: {image_model_id} | {size}")

        # 2. API Request
        try:
            resp = requests.post(
                ONE_MIN_FEATURES_URL,
                headers=_one_min_headers(api_key),
                json={
                    "type": "IMAGE_GENERATOR",
                    "model": image_model_id,
                    "promptObject": prompt_object,
                },
                timeout=(10, 180),
            )
            _raise_for_1min_status(resp, "1min.ai image request failed")
        except Exception as e:
            raise RuntimeError(f"Image generation failed: {e}")

        # 3. Download and Process Image
        image_url = _extract_1min_image_url(resp.json())
        if not image_url:
            raise RuntimeError("1min.ai returned no usable image URL.")

        r = requests.get(image_url, timeout=(10, 60))
        r.raise_for_status()
        img = Image.open(BytesIO(r.content)).convert("RGB")

        # 4. Final Formatting (Scaling/Padding)
        target_dimensions = (w, h)
        if _setting_enabled(settings, "padImage"):
            if settings.get("backgroundOption") == "blur":
                return pad_image_blur(img, target_dimensions)
            else:
                bg_hex = settings.get("backgroundColor") or "#ffffff"
                background_color = ImageColor.getcolor(bg_hex, "RGB")
                return ImageOps.pad(img, target_dimensions, color=background_color, method=Image.Resampling.LANCZOS)

        # Default to direct resize if padding isn't requested
        return img.resize(target_dimensions, Image.Resampling.LANCZOS)
