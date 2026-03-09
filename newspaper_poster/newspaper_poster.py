import base64
import json
import logging
import os
import re
import tempfile
import time
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path

import requests
from flask import abort, jsonify, request
from groq import Groq
from openai import BadRequestError, OpenAI
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

VIBES_FILE = Path(__file__).parent / "vibes.json"

# Model registry - Image = Open Ai, Front Page Analysis = llama or GPT-4o
MODEL_CATALOG = {
    "image": {
        "gpt-image-1-mini": {"label": "Image 1 Mini (OpenAI)", "provider": "openai"},
        "gpt-image-1":      {"label": "Image 1 (OpenAI)",      "provider": "openai"},
        "gpt-image-1.5":    {"label": "Image 1.5 (OpenAI)",    "provider": "openai"},
    },
    "analysis": {
        "meta-llama/llama-4-scout-17b-16e-instruct": {"label": "Llama-4 (Groq)",       "provider": "groq"},
        "gpt-4o":                                   {"label": "ChatGPT-4o (OpenAI)",  "provider": "openai"},
    },
}

DEFAULT_MODELS = {
    "image": "gpt-image-1-mini",
    # default headline analysis to ChatGPT-4o (OpenAI) - But llama is better at pulling headlines.
    "analysis": os.getenv("VISION_MODEL", "gpt-4o").strip(),
}

# Fixes Issue with the api key appearing blank and not rendering the drop-downs properly 
def _has_key(v) -> bool:
    return bool(v and str(v).strip())

# Choose the user-selected image/analysis model from settings
def _pick_model(settings: dict, kind: str):
    key = "imageModel" if kind == "image" else "analysisModel"
    model_id = (settings.get(key) or DEFAULT_MODELS.get(kind, "") or "").strip()

    if model_id not in MODEL_CATALOG[kind]:
        logger.warning(f"Unknown {kind} model '{model_id}', defaulting to {DEFAULT_MODELS.get(kind)}")
        model_id = (DEFAULT_MODELS.get(kind) or "").strip()

    if model_id not in MODEL_CATALOG[kind]:
        model_id = next(iter(MODEL_CATALOG[kind].keys()))
        logger.warning(f"{kind} default also invalid; falling back to first catalog entry: {model_id}")

    return model_id, MODEL_CATALOG[kind][model_id]

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
    if plugin_id != "newspaper_poster":
        abort(404)
    vibes = _sorted(_read_vibes())
    resp = jsonify({"ok": True, "vibes": vibes})
    # discourage caching so UI always reflects disk
    resp.headers["Cache-Control"] = "no-store"
    return resp

# Add vibe
@plugin_bp.post("/plugin/<plugin_id>/vibes/add")
def vibes_add(plugin_id):
    if plugin_id != "newspaper_poster":
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
    if plugin_id != "newspaper_poster":
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

        template_params["api_key_openai"] = {
            "required": True,
            "service": "OpenAI",
            "expected_key": "OPEN_AI_SECRET",
        }
        template_params["api_key_groq"] = {
            "required": True,
            "service": "Groq",
            "expected_key": "GROQ_API_KEY",
        }

        # required by settings.html
        template_params["newspapers"] = NEWSPAPERS

        # ---- key presence (dotenv already loaded above) ----
        groq_key = os.getenv("GROQ_API_KEY")
        openai_key = os.getenv("OPENAI_API_KEY") or os.getenv("OPEN_AI_SECRET")

        # ---- IMAGE models ----
        image_models = []
        for model_id, meta in MODEL_CATALOG["image"].items():
            provider = (meta or {}).get("provider")
            if provider == "openai" and not _has_key(openai_key):
                continue
            image_models.append({"id": model_id, "label": meta.get("label", model_id)})
        template_params["image_models"] = image_models  # always present

        # ---- ANALYSIS models ----
        analysis_models = []
        for model_id, meta in MODEL_CATALOG["analysis"].items():
            provider = (meta or {}).get("provider")
            if provider == "groq" and not _has_key(groq_key):
                continue
            if provider == "openai" and not _has_key(openai_key):
                continue
            analysis_models.append({"id": model_id, "label": meta.get("label", model_id)})
        template_params["analysis_models"] = analysis_models  # always present

        return template_params

    def get_analysis_client(self, device_config, model_meta):
        provider = (model_meta or {}).get("provider")
        if provider == "groq":
            api_key = device_config.load_env_key("GROQ_API_KEY")
            return Groq(api_key=api_key) if api_key else None

        if provider == "openai":
            api_key = (
                device_config.load_env_key("OPEN_AI_SECRET")
                or device_config.load_env_key("OPENAI_API_KEY")
            )
            return OpenAI(api_key=api_key) if api_key else None

        return None
    

    # Grab that headline and article - or at least try by analyzing front page 

    def _looks_blocked_or_useless(self, parsed: dict, raw_text: str) -> bool:
        headline = (parsed.get("headline") or "").strip()
        article = (parsed.get("article") or "").strip()
        raw = (raw_text or "").strip()

        if not headline:
            return True

        refusal_markers = [
            "i'm sorry", "i'm unable", "i cannot assist", "i can’t assist",
            "i can't assist", "i can't help", "i cannot help", 
            "can't help with that", "cannot help with that",
            "unable to comply", "copyright"
        ]

        low_article = article.lower()
        low_raw = raw.lower()
        
        if any(m in low_article for m in refusal_markers):
            return True
        if any(m in low_raw for m in refusal_markers) and len(raw) < 300:
            return True

        return False
    
    def analyze_front_page(self, image_url: str, model_id: str, model_meta: dict, device_config):
        # YOUR ORIGINAL PROMPT - Fully Restored
        prompt_text = (
            "Look at the front page image and do TWO tasks.\n"
            "1) Extract the single MAIN banner headline.\n"
            "2) Find the matching article blurb on the front page and rewrite it as ONE paragraph.\n\n"
            "OUTPUT FORMAT (follow exactly):\n"
            "HEADLINE: <headline text>\n"
            "ARTICLE: <one-paragraph article blurb>\n\n"
            "RULES:\n"
            "- Headline must be ONLY the headline words (no colon, no extra text).\n"
            "- ARTICLE must NOT repeat the headline.\n"
            "- Do not include the newspaper name, date, bylines, section labels, or subheadlines.\n"
        )

        client = self.get_analysis_client(device_config, model_meta)
        if not client:
            logger.warning("No analysis client could be initialized.")
            return None

        provider = (model_meta or {}).get("provider")

        try:
            if provider == "groq":
                # Groq OpenAI-compat vision call
                resp = client.chat.completions.create(
                    model=model_id,
                    messages=[{
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt_text},
                            {"type": "image_url", "image_url": {"url": image_url}},
                        ],
                    }],
                    max_tokens=600,
                )
                text = (resp.choices[0].message.content or "").strip()

            elif provider == "openai":
                # OpenAI multimodal uses Responses API
                resp = client.responses.create(
                    model=model_id,  # e.g. "gpt-4o"
                    input=[{
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": prompt_text},
                            {"type": "input_image", "image_url": image_url},
                        ],
                    }],
                    max_output_tokens=600,
                )
                text = (resp.output_text or "").strip()

            else:
                logger.warning("Unknown analysis provider: %s", provider)
                return None

            if not text:
                logger.warning("Analysis returned empty text.")
                return None

            parsed = self._parse_headline_article(text)

            print("\n" + "=" * 60)
            print(f"DEBUG ANALYSIS RESULT ({provider.upper()})")
            print("HEADLINE:", parsed.get("headline"))
            print("ARTICLE:",  parsed.get("article"))
            print("=" * 60 + "\n")

            if self._looks_blocked_or_useless(parsed, text):
                logger.warning("Analysis blocked/refused or unusable for %s", image_url)
                return None

            return parsed

        except Exception as e:
            logger.exception("Image analysis failed: %s", e)
            return None


    # Parse the headline and article from the above raw
    def _parse_headline_article(self, text: str) -> dict:
        headline = ""
        article_lines = []
        in_article = False

        for raw in (text or "").splitlines():
            line = raw.strip()
            if not line:
                continue

            if line.startswith("HEADLINE:"):
                headline = line.replace("HEADLINE:", "", 1).strip()
                in_article = False
                continue

            if line.startswith("ARTICLE:"):
                rest = line.replace("ARTICLE:", "", 1).strip()
                if rest:
                    article_lines.append(rest)
                in_article = True
                continue

            if in_article:
                article_lines.append(line)

        article = " ".join(article_lines).strip()

        if not headline and not article:
            return {"headline": "", "article": text or ""}

        return {"headline": headline, "article": article}

    # Build prompt to sends to image model: Headline, article, poster rules, text safety rules, and vibes
    def build_ai_prompt(self, vibe_id: str, analysis: dict) -> str:
        analysis = analysis or {}
        headline = (analysis.get("headline") or "").strip()
        article  = (analysis.get("article")  or "").strip()

        if not headline and not article:
            headline = "UNKNOWN HEADLINE"
            article = "No article text extracted from the front page."

        if isinstance(POSTER_RULES, dict):
            rules_text = "\n".join(f"- {k}: {v}" for k, v in POSTER_RULES.items())
        else:
            rules_text = str(POSTER_RULES).strip()

        vibe_text = _get_vibe_description(vibe_id)

       
        story_block = (
            "STORY CONTENT TO USE:\n"
            f"HEADLINE: {headline}\n"
            f"ARTICLE (summary / blurb): {article}\n"
        )

        final_prompt = (
            f"{(vibe_text + '\n\n') if vibe_text else ''}"
            f"{rules_text}\n\n"
            f"{story_block}\n"
          
        ).strip()

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

        for date in days:
            image_url = FREEDOM_FORUM_URL.format(date.day, newspaper_slug)
            
            try:
                # Use a head request to see if the image exists without downloading it
                check = requests.head(image_url, timeout=(5, 15), allow_redirects=True)
                
                if check.status_code == 200:
                    logger.info(f"Found {newspaper_slug} for day {date.day}. Starting analysis...")
                    
                    analysis_model_id, analysis_meta = _pick_model(settings, "analysis")
                    
                    # Call analysis with the required device_config argument
                    analysis = self.analyze_front_page(image_url, analysis_model_id, analysis_meta, device_config)

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
                "Could not extract headline/article text from the front page. "
                "The AI may be blocking this specific paper, or the archive is unavailable."
            )

        # 3. Build Prompt and Generate Final Image
        vibe_id = settings.get("vibe_id")
        ai_prompt = self.build_ai_prompt(vibe_id, analysis)
        
        return self.generate_openai_image(ai_prompt, settings, device_config)

    #Render (prompt → OpenAI image → fit to screen).
    def generate_openai_image(self, ai_prompt: str, settings, device_config) -> Image.Image:
        """
        Communicates with OpenAI to generate the poster art based on the analyzed headline.
        """
        # Load API Key via framework standard
        api_key = (
        device_config.load_env_key("OPEN_AI_SECRET")
        or device_config.load_env_key("OPENAI_API_KEY")
    )   
        if not api_key:
            raise RuntimeError("OpenAI API key not found (OPEN_AI_SECRET / OPENAI_API_KEY).")

        # 1. Resolve Model and Display Dimensions
        image_model_id, image_meta = _pick_model(settings, "image")
        orientation = (device_config.get_config("orientation") or "horizontal").lower()
        w, h = device_config.get_resolution()

        # Handle rotation/orientation for target dimensions
        if orientation == "vertical" and w > h:
            w, h = h, w
        elif orientation == "horizontal" and h > w:
            w, h = h, w

        # Map to OpenAI supported aspect ratios
        size = "1536x1024" if orientation == "horizontal" else "1024x1536"
        
        # Initialize client locally to keep memory footprint low
        client = OpenAI(api_key=api_key)

        logger.info(f"Generating image: {image_model_id} | {size}")

        # 2. API Request with Safety Handling
        try:
            resp = client.images.generate(
                model=image_model_id,
                prompt=ai_prompt,
                size=size,
                n=1,
            )
        except BadRequestError as e:
            err = getattr(e, "body", None) or {}
            err_obj = err.get("error", {}) if isinstance(err, dict) else {}
            if err_obj.get("code") == "moderation_blocked":
                raise RuntimeError("OpenAI safety system blocked this prompt/headline.")
            raise RuntimeError(f"OpenAI Error: {e}")
        except Exception as e:
            raise RuntimeError(f"Image generation failed: {e}")

        # 3. Download and Process Image
        img_data = resp.data[0]
        if hasattr(img_data, "b64_json") and img_data.b64_json:
            img = Image.open(BytesIO(base64.b64decode(img_data.b64_json))).convert("RGB")
        elif hasattr(img_data, "url") and img_data.url:
            # Using standard requests here; framework session is also an option
            r = requests.get(img_data.url, timeout=20)
            r.raise_for_status()
            img = Image.open(BytesIO(r.content)).convert("RGB")
        else:
            raise RuntimeError("API returned no usable image data.")

        # 4. Final Formatting (Scaling/Padding)
        target_dimensions = (w, h)
        if settings.get("padImage") == "true":
            if settings.get("backgroundOption") == "blur":
                return pad_image_blur(img, target_dimensions)
            else:
                bg_hex = settings.get("backgroundColor") or "#ffffff"
                background_color = ImageColor.getcolor(bg_hex, "RGB")
                return ImageOps.pad(img, target_dimensions, color=background_color, method=Image.Resampling.LANCZOS)

        # Default to direct resize if padding isn't requested
        return img.resize(target_dimensions, Image.Resampling.LANCZOS)
