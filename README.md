# Recipe API

A simple Flask API that extracts recipe data from URLs using schema.org Recipe markup when available, falling back to basic text parsing.

## Endpoints
- `/parse?url=<recipe_url>`: Returns extracted recipe data in JSON format.

## Deployment
This app is ready to be deployed to services like Render or Heroku.
