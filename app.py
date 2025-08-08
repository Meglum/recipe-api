from flask import Flask, request, jsonify
import requests
import extruct
from w3lib.html import get_base_url
from bs4 import BeautifulSoup

app = Flask(__name__)

def extract_recipe_data(url):
    headers = {"User-Agent": "Mozilla/5.0"}
    response = requests.get(url, headers=headers, timeout=10)
    html = response.text
    base_url = get_base_url(response.url)

    # Try JSON-LD & microdata
    metadata = extruct.extract(html, base_url=base_url, syntaxes=["json-ld", "microdata"], uniform=True)
    for syntax in ["json-ld", "microdata"]:
        for data in metadata.get(syntax, []):
            if data.get("@type") in ["Recipe", ["Recipe"]]:
                return {
                    "name": data.get("name"),
                    "ingredients": data.get("recipeIngredient"),
                    "instructions": [step.get("text", step) for step in data.get("recipeInstructions", [])]
                }

    # Fallback: HTML parsing
    soup = BeautifulSoup(html, "html.parser")
    ingredients = [li.get_text(strip=True) for li in soup.select("li") if "ingredient" in li.get("class", [])]
    instructions = [p.get_text(strip=True) for p in soup.select("p") if "step" in p.get("class", [])]

    if ingredients or instructions:
        return {
            "name": soup.title.string if soup.title else "Recipe",
            "ingredients": ingredients,
            "instructions": instructions
        }

    return None

@app.route("/extract", methods=["GET"])
def extract():
    url = request.args.get("url")
    if not url:
        return jsonify({"error": "Missing URL"}), 400

    try:
        recipe = extract_recipe_data(url)
        if recipe:
            return jsonify(recipe)
        else:
            return jsonify({"error": "Failed to decode recipe"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
