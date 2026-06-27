import requests
import json
import os
import time
from datetime import datetime
from openai import OpenAI

# --- CONFIGURATION ---
API_ENDPOINT = "https://seo-phase.emergent.host/api/ingest/restaurants"
# GitHub Secrets will provide these
API_KEY = os.getenv("INGEST_API_KEY")
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

    def generate_ai_content(self, name, city, cuisine):
        """Uses AI to write the description and paint the image."""
        print(f"Generating AI content for {name} in {city}...")
        
        # 1. Rewrite Description
        prompt = f"Write a catchy, 2-sentence description for a restaurant named '{name}' in {city}. It serves {cuisine}. Make it sound like a local Alberta food expert wrote it for a culinary road trip guide. No hashtags."
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": prompt}]
        )
        description = response.choices[0].message.content

        # 2. Paint AI Image
        image_prompt = f"Professional food photography of a signature dish at '{name}', a {cuisine} restaurant in {city}, Alberta. High-end lighting, appetizing, rustic yet modern atmosphere, 4k resolution."
        image_response = client.images.generate(
            model="dall-e-3",
            prompt=image_prompt,
            size="1024x1024",
            quality="standard",
            n=1,
        )
        image_url = image_response.data[0].url
        
        return description, image_url

    def scrub_sources(self):
        """A fresh batch of unique Alberta gems."""
        print(f"[{datetime.now()}] Starting Alberta Culinary Scrub...")
        
        raw_data = [
            {"name": "The Water Tower", "city": "Lethbridge", "cuisine": "Upscale Canadian"},
            {"name": "Stockmen's Chophouse", "city": "Camrose", "cuisine": "Alberta Steakhouse"},
            {"name": "Blueberry Hill", "city": "Viking", "cuisine": "Ukrainian / Diner"}
        ]
        
        candidates = []
        for item in raw_data:
            desc, img = self.generate_ai_content(item['name'], item['city'], item['cuisine'])
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
