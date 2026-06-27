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
        food_images = {
            "Upscale Canadian": "https://images.unsplash.com/photo-1414235077428-338989a2e8c0?w=800&h=600&fit=crop",
            "Alberta Steakhouse": "https://images.unsplash.com/photo-1544025162-d76694265947?w=800&h=600&fit=crop",
            "Ukrainian / Diner": "https://images.unsplash.com/photo-1504674900247-0877df9cc836?w=800&h=600&fit=crop",
            "Japanese": "https://images.unsplash.com/photo-1579871494447-9811cf80d66c?w=800&h=600&fit=crop",
            "Italian": "https://images.unsplash.com/photo-1498050108023-c5249f4df085?w=800&h=600&fit=crop",
            "French": "https://images.unsplash.com/photo-1555396273-367ea4eb4db5?w=800&h=600&fit=crop",
            "Mexican": "https://images.unsplash.com/photo-1565299585323-38d6b0865b47?w=800&h=600&fit=crop",
            "Korean": "https://images.unsplash.com/photo-1590301157890-4810ed352733?w=800&h=600&fit=crop",
            "Cafe": "https://images.unsplash.com/photo-1495474472287-4d71bcdd2085?w=800&h=600&fit=crop",
            "Bakery": "https://images.unsplash.com/photo-1509440159596-0249088772ff?w=800&h=600&fit=crop",
            "Cocktail": "https://images.unsplash.com/photo-1514362545857-3bc16c4c7d1b?w=800&h=600&fit=crop",
            "Latin": "https://images.unsplash.com/photo-1565299585323-38d6b0865b47?w=800&h=600&fit=crop",
            "default": "https://images.unsplash.com/photo-1517248135467-4c7edcad34c4?w=800&h=600&fit=crop"
        }
        for key in food_images:
            if key.lower( ) in cuisine.lower():
                return food_images[key]
        return food_images["default"]

    def scrub_sources(self):
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
