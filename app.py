from flask import Flask, request, jsonify
import requests
from bs4 import BeautifulSoup
import extruct
from w3lib.html import get_base_url

app = Flask(__name__)

def extract_recipe(url):
    headers = {'User-Agent': 'Mozilla/5.0'}
    r = requests.get(url, headers=headers)
    r.raise_for_status()
    base_url = get_base_url(r.text, r.url)

    # Try to extract structured data first
    data = extruct.extract(r.text, base_url=base_url, syntaxes=['json-ld'])
    for item in data.get("json-ld", []):
        if item.get('@type') == 'Recipe':
            return item

    # Fallback to text parsing
    soup = BeautifulSoup(r.text, 'html.parser')
    recipe = {
        "name": soup.find('h1').get_text(strip=True) if soup.find('h1') else None,
        "ingredients": [li.get_text(strip=True) for li in soup.select('li') if 'ingredient' in li.get_text().lower()],
        "instructions": [p.get_text(strip=True) for p in soup.find_all('p')]
    }
    return recipe

@app.route('/parse', methods=['GET'])
def parse():
    url = request.args.get('url')
    if not url:
        return jsonify({"error": "URL is required"}), 400

    try:
        recipe = extract_recipe(url)
        if not recipe:
            return jsonify({"error": "Recipe not found"}), 404
        return jsonify(recipe)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
