# app.py
from flask import Flask, request, jsonify
import os, re, json, requests
from bs4 import BeautifulSoup
import extruct
from w3lib.html import get_base_url

# Optional Cloudflare/Akamai bypass
try:
    import cloudscraper  # pip install cloudscraper
except Exception:
    cloudscraper = None

app = Flask(__name__)

# ------------ HTTP headers ------------
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Connection": "keep-alive",
}

# ------------ Helpers (original + new) ------------
def _clean(s):
    if not s:
        return ""
    return re.sub(r"\s+", " ", str(s)).strip()

def _as_list(x):
    if x is None:
        return []
    if isinstance(x, list):
        return [_clean(i) for i in x if _clean(i)]
    return [_clean(x)]

def _flatten_instructions(instr):
    """
    Accepts:
      - list[str]
      - list[dict] HowToStep/HowToSection (possibly nested)
      - str or dict with 'text'/'name'
    Returns flat list[str]
    """
    out = []
    if not instr:
        return out
    if isinstance(instr, str):
        t = _clean(instr)
        return [t] if t else []
    if isinstance(instr, list):
        for item in instr:
            if isinstance(item, str):
                t = _clean(item)
                if t:
                    out.append(t)
            elif isinstance(item, dict):
                t = _clean(item.get("text") or item.get("name"))
                if t:
                    out.append(t)
                # HowToSection may have "itemListElement"
                children = item.get("itemListElement")
                if children:
                    out.extend(_flatten_instructions(children))
    elif isinstance(instr, dict):
        t = _clean(instr.get("text") or instr.get("name"))
        if t:
            out.append(t)
        children = instr.get("itemListElement")
        if children:
            out.extend(_flatten_instructions(children))
    return out

def _pick_image(image_field):
    # image can be str, list[str], dict with url/@id
    if not image_field:
        return ""
    if isinstance(image_field, str):
        return _clean(image_field)
    if isinstance(image_field, list):
        for it in image_field:
            if isinstance(it, str) and _clean(it):
                return _clean(it)
            if isinstance(it, dict):
                url = it.get("url") or it.get("@id") or it.get("contentUrl")
                if url:
                    return _clean(url)
        return ""
    if isinstance(image_field, dict):
        url = image_field.get("url") or image_field.get("@id") or image_field.get("contentUrl")
        return _clean(url)
    return ""

def _find_recipe_nodes(extruct_data):
    """
    Returns list of possible Recipe dicts from various shapes:
    - top-level json-ld items
    - items inside @graph arrays
    - microdata items
    """
    candidates = []

    def consider(obj):
        if not isinstance(obj, dict):
            return
        t = obj.get("@type")
        types = t if isinstance(t, list) else [t]
        if any(tt and str(tt).lower() == "recipe" for tt in types):
            candidates.append(obj)

    # json-ld
    for item in extruct_data.get("json-ld", []) or []:
        if isinstance(item, dict):
            consider(item)
            graph = item.get("@graph")
            if isinstance(graph, list):
                for g in graph:
                    consider(g)
        elif isinstance(item, list):
            for it in item:
                consider(it)

    # microdata
    for item in extruct_data.get("microdata", []) or []:
        consider(item)

    return candidates

# ---------- NEW: time & yield utilities ----------
# Matches "15-20 minutes", "1–2 hrs", "5 min", "90 m", etc.
TIME_RE = re.compile(
    r"(\d{1,3})(?:\s*(?:-|–|to)\s*(\d{1,3}))?\s*"
    r"(h|hr|hrs|hour|hours|m|min|mins|minute|minutes)\b",
    re.I,
)

def _format_minutes(m):
    try:
        m = int(m)
    except Exception:
        return None
    return f"{m//60} h" if m >= 60 else f"{m} min"

def format_duration(raw):
    """
    Accepts:
      - ISO 8601: PT1H30M, PT50M, PT3600S
      - digits: "45" (treated as minutes)
      - phrases: "1 hr 20 min", "45 minutes"
    Returns canonical "90 min" or "1 h" style strings.
    """
    if not raw:
        return None
    s = _clean(raw).upper()

    # digits → minutes
    if re.fullmatch(r"\d+", s):
        return _format_minutes(int(s))

    # ISO 8601 PT durations
    if s.startswith("PT"):
        h = re.search(r"(?<=PT)\d+(?=H)", s)
        m = re.search(r"\d+(?=M)", s)
        ss = re.search(r"\d+(?=S)", s)
        hours = int(h.group(0)) if h else 0
        mins = int(m.group(0)) if m else 0
        # if only seconds, approximate to minutes (min 1)
        if hours == 0 and mins == 0 and ss:
            secs = int(ss.group(0))
            mins = max(1, round(secs / 60))
        total = hours * 60 + mins
        return _format_minutes(total)

    # time phrase → take the lower bound with units
    m = TIME_RE.search(s)
    if m:
        num1 = int(m.group(1))
        unit = m.group(3).lower()
        mins = num1 * 60 if unit.startswith("h") else num1
        return _format_minutes(mins)

    return raw  # fallback: already human-readable

def normalize_yield(raw):
    """
    Normalize yields like "Serves 4", "4 – 6", "4-6", "Makes 12" → "4", "4-6", "12"
    """
    if not raw:
        return None
    s = _clean(str(raw)).lower()
    m = re.search(r"\d+(?:\s*[-–]\s*\d+)?", s)
    return m.group(0).replace(" ", "") if m else s

def derive_cook_from_steps(steps):
    """
    Sum all time mentions across all step sentences. e.g., "5 mins" + "2 mins" → "7 min"
    """
    if not steps:
        return None
    total = 0
    for st in steps:
        for m in TIME_RE.finditer(st):
            low = int(m.group(1))
            unit = m.group(3).lower()
            total += (low * 60) if unit.startswith("h") else low
    return _format_minutes(total) if total > 0 else None

# ------------ Extract via schema.org first ------------
def extract_schema_recipe(html, url):
    base_url = get_base_url(html, url)
    data = extruct.extract(
        html,
        base_url=base_url,
        syntaxes=["json-ld", "microdata"],
        uniform=True,
    )
    recipes = _find_recipe_nodes(data)
    if not recipes:
        return None

    r = recipes[0]  # take the first match
    title = _clean(r.get("name"))
    ingredients = _as_list(r.get("recipeIngredient") or r.get("ingredients"))
    steps = _flatten_instructions(r.get("recipeInstructions"))

    out = {
        "title": title or "",
        "ingredients": ingredients,
        "steps": steps,
        "image": _pick_image(r.get("image")) or None,
        "prepTime": format_duration(r.get("prepTime") or r.get("prep_time")),
        "cookTime": format_duration(r.get("cookTime") or r.get("cook_time") or r.get("totalTime") or r.get("total_time")),
        "recipeYield": normalize_yield(r.get("recipeYield")),
    }

    # If a recipe node has nothing meaningful, treat as missing
    if not out["title"] and not out["ingredients"] and not out["steps"]:
        return None
    return out

# ------------ HTML fallback if schema.org missing ------------
INGR_HEADINGS = re.compile(r"\b(ingredients|ingredient list|you(?:’|'|)ll need|what you'll need|shopping list)\b", re.I)
STEP_HEADINGS = re.compile(r"\b(method|steps?|instructions?|preparation|directions?|how to(?: make)?)\b", re.I)

def _next_list_items(node):
    # Find the next ul/ol after a heading-like node
    for sib in node.find_all_next(["ul", "ol"], limit=2):
        items = [_clean(li.get_text(" ", strip=True)) for li in sib.find_all("li")]
        items = [i for i in items if i]
        if items:
            return items
    return []

def _numbered_paragraphs(soup):
    out = []
    for p in soup.find_all("p"):
        txt = _clean(p.get_text(" ", strip=True))
        if re.match(r"^\d+[\.\)]\s+", txt) or (len(txt.split()) > 6 and re.search(r"\bstep\b", txt.lower())):
            out.append(txt)
    return out

def _scan_labels_for_times_and_serves(plain_text):
    """
    From the page plain text, try to find:
      - Prep: ...
      - Cook: ... OR Total/Ready in: ...
      - Serves/Servings/Yield/Makes: ...
    Return dict with strings (already normalized when possible)
    """
    t = " ".join(plain_text.split())  # collapse whitespace
    lower = t.lower()

    def value_after(label_regex):
        m = re.search(label_regex, lower, re.I)
        if not m:
            return None
        # take a short slice after the label
        slice_ = lower[m.end(): m.end() + 60]
        # prefer a time phrase
        m_time = TIME_RE.search(slice_)
        if m_time:
            num1 = int(m_time.group(1))
            unit = m_time.group(3).lower()
            mins = num1 * 60 if unit.startswith("h") else num1
            return _format_minutes(mins)
        # otherwise return an early token (e.g., "4", "4-6")
        m_yield = re.search(r"^\s*(\d+(?:\s*[-–]\s*\d+)?)", slice_)
        if m_yield:
            return m_yield.group(1).replace(" ", "")
        return None

    prep = value_after(r"\bprep\s*:?\s*")
    cook = value_after(r"\b(cook|cooking)\s*:?\s*")
    total = value_after(r"\b(total|ready\s*in)\s*:?\s*")
    serves = value_after(r"\b(serves?|servings?|yield|makes)\s*:?\s*")

    return {
        "prepTime": prep,
        "cookTime": cook or total,
        "recipeYield": serves,
    }

def extract_html_fallback(html):
    soup = BeautifulSoup(html, "html.parser")

    # Title
    h = soup.find("h1") or soup.find("h2") or soup.title
    title = _clean(h.get_text(" ", strip=True) if h else "")

    # Ingredients by heading → next list
    ingredients = []
    hdr = soup.find(string=INGR_HEADINGS)
    if hdr and hasattr(hdr, "parent"):
        ingredients = _next_list_items(hdr.parent)

    # If empty, try common class names
    if not ingredients:
        guess_lists = soup.select("[class*=ingredient] li, .ingredients li, .recipe-ingredients li")
        ingredients = [_clean(li.get_text(" ", strip=True)) for li in guess_lists if _clean(li.get_text(" ", strip=True))]
    # As an absolute last resort, take first short-ish UL
    if not ingredients:
        for ul in soup.find_all("ul"):
            items = [_clean(li.get_text(" ", strip=True)) for li in ul.find_all("li")]
            items = [i for i in items if 2 <= len(i.split()) <= 25]
            if 4 <= len(items) <= 40:  # heuristics
                ingredients = items
                break

    # Steps by heading → next ordered list OR numbered paragraphs
    steps = []
    sh = soup.find(string=STEP_HEADINGS)
    if sh and hasattr(sh, "parent"):
        for sib in sh.parent.find_all_next(["ol", "ul"], limit=2):
            items = [_clean(li.get_text(" ", strip=True)) for li in sib.find_all("li")]
            items = [i for i in items if i]
            if items:
                steps = items
                break

    # If still nothing, try common class names
    if not steps:
        guess_steps = soup.select("[class*=method] li, .method__item, .instructions li, .direction li, .directions li")
        steps = [_clean(el.get_text(" ", strip=True)) for el in guess_steps if _clean(el.get_text(" ", strip=True))]

    # Finally, numbered paragraphs
    if not steps:
        steps = _numbered_paragraphs(soup)

    # Times/serves from labels across body text
    labels = _scan_labels_for_times_and_serves(soup.get_text(" ", strip=True))

    return {
        "title": title or "",
        "ingredients": ingredients or [],
        "steps": steps or [],
        "prepTime": labels.get("prepTime"),
        "cookTime": labels.get("cookTime"),
        "recipeYield": labels.get("recipeYield"),
    }

# ------------ Anti-bot aware fetcher (unchanged behavior) ------------
def fetch_html(url: str):
    """
    Returns (html, final_url). Tries:
      1) requests with realistic headers
      2) cloudscraper (if installed)
      3) ScraperAPI (if SCRAPER_API_KEY env set)
    Raises Exception if all fail.
    """
    last_err = None

    # 1) requests
    try:
        r = requests.get(url, headers=DEFAULT_HEADERS, timeout=25, allow_redirects=True)
        if r.status_code < 400 and r.text:
            return r.text, r.url
        if r.status_code not in (403, 429, 503):
            body = (r.text or "")[:300]
            raise Exception(f"HTTP {r.status_code}. Snippet: {body}")
    except requests.RequestException as e:
        last_err = f"requests error: {e}"

    # 2) cloudscraper
    if cloudscraper is not None:
        try:
            scraper = cloudscraper.create_scraper(browser={"browser": "chrome", "platform": "windows", "mobile": False})
            r = scraper.get(url, headers=DEFAULT_HEADERS, timeout=35, allow_redirects=True)
            if r.status_code < 400 and r.text:
                return r.text, r.url
            if r.status_code not in (403, 429, 503):
                body = (r.text or "")[:300]
                last_err = f"cloudscraper HTTP {r.status_code}. Snippet: {body}"
        except Exception as e:
            last_err = f"cloudscraper error: {e}"

    # 3) ScraperAPI (optional)
    key = os.environ.get("SCRAPER_API_KEY")
    if key:
        try:
            proxy_url = "https://api.scraperapi.com"
            params = {
                "api_key": key,
                "url": url,
                "keep_headers": "true",
                # AU region can help for Australian sites like taste.com.au
                "country_code": os.environ.get("SCRAPER_COUNTRY", "au"),
                # "render": "true",  # uncomment if you need JS rendering
            }
            r = requests.get(proxy_url, params=params, headers=DEFAULT_HEADERS, timeout=60)
            if r.status_code < 400 and r.text:
                return r.text, url
            body = (r.text or "")[:300]
            last_err = f"ScraperAPI HTTP {r.status_code}. Snippet: {body}"
        except Exception as e:
            last_err = f"scraperapi error: {e}"

    raise Exception(f"Fetch failed for url: {url}. {last_err or ''}")

# ------------ HTTP endpoint ------------
@app.route("/extract", methods=["GET"])
def extract():
    url = request.args.get("url")
    if not url:
        return jsonify({"error": "Missing url parameter"}), 400

    try:
        html, final_url = fetch_html(url)
    except Exception as e:
        return jsonify({"error": f"Fetch failed: {e}"}), 502

    # Try schema.org first
    try:
        data = extract_schema_recipe(html, final_url)
    except Exception:
        data = None

    # Fallback to HTML heuristics
    if not data or (not data.get("ingredients") and not data.get("steps")):
        data = extract_html_fallback(html)

    # Final safety: derive cook from steps if still missing
    if not data.get("cookTime") and data.get("steps"):
        derived = derive_cook_from_steps(data.get("steps"))
        if derived:
            data["cookTime"] = derived

    # Guarantee the shape the iOS app expects
    safe = {
        "title": data.get("title", "") or "",
        "ingredients": data.get("ingredients") or [],
        "steps": data.get("steps") or [],
        "image": data.get("image") or None,
        "prepTime": data.get("prepTime") or None,
        "cookTime": data.get("cookTime") or None,
        "recipeYield": data.get("recipeYield") or None,
        # "nutrition": {...}  # can be added later
    }
    return jsonify(safe)

@app.route("/health")
def health():
    return {"ok": True}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
