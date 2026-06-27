import requests
import json
import os
import time
from datetime import datetime
from openai import OpenAI

# --- CONFIGURATION ---
API_ENDPOINT = "https://seo-phase.emergent.host/api/ingest/restaurants"
API_KEY = os.getenv("INGEST_API_KEY" )
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

client = OpenAI(api_key=OPENAI_API_KEY)

class AlbertaCulinaryScraper:
    def __init__(self, api_endpoint, api_key):
        self.api_endpoint = api_endpoint
        self.api_key = api_key
        self.headers = {
            "Content-Type": "application/json",
            "X-API-Key": self.api_key
        }

    def generate_description(self, name, city, cuisine):
        print(f"Writing description for {name} in {city}...")
        prompt = f"Write a catchy, 2-sentence description for a restaurant named '{name}' in {city}. It serves {cuisine}. Make it sound like a local Alberta food expert wrote it for a culinary road trip guide. No hashtags."
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": prompt}]
        )
        return response.choices[0].message.content

    def get_food_image(self, cuisine):
        keyword = cuisine.split("/")[0].strip().replace(" ", "-").lower()
        return f"https://source.unsplash.com/800x600/?{keyword},food,restaurant"

    def scrub_sources(self ):
        print(f"[{datetime.now()}] Starting Alberta Culinary Scrub...")
        
        raw_data = [
            {"name": "The Water Tower", "city": "Lethbridge", "cuisine": "Upscale Canadian"},
            {"name": "Stockmen's Chophouse", "city": "Camrose", "cuisine": "Alberta Steakhouse"},
            {"name": "Blueberry Hill", "city": "Viking", "cuisine": "Ukrainian / Diner"}
        ]
        
        candidates = []
        for item in raw_data:
            desc = self.generate_description(item['name'], item['city'], item['cuisine'])
            img = self.get_food_image(item['cuisine'])
            candidates.append({
                "name": item['name'],
                "city": item['city'],
                "cuisine": item['cuisine'],
                "price": "$$$",
                "description": desc,
                "image_url": img,
                "tags": ["Trending", "Hidden Gem", "AI-Curated"],
                "is_trending": True,
                "confidence": 0.98,
                "status": "pending"
            })
        return candidates

    def push_to_culinera(self, candidates):
        if not candidates: return
        payload = {"candidates": candidates}
        try:
            response = requests.post(self.api_endpoint, headers=self.headers, json=payload)
            print(f"Pushing to Culinera... Status: {response.status_code}")
            print(f"Response: {response.json()}")
        except Exception as e:
            print(f"Error: {e}")

if __name__ == "__main__":
    scraper = AlbertaCulinaryScraper(API_ENDPOINT, API_KEY)
    new_gems = scraper.scrub_sources()
    scraper.push_to_culinera(new_gems)
    print("\nProcess Complete!")
