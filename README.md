# Recipe API

A Flask API to extract recipe data from any webpage.

## Endpoints
- `/extract?url=RECIPE_URL` → Returns JSON with recipe name, ingredients, and instructions.

## Deployment
- Install dependencies: `pip install -r requirements.txt`
- Run locally: `python app.py`
- Deploy on Render/Railway with `web: gunicorn app:app`
