import os
import json
import asyncio
import aiohttp
from datetime import datetime

# NOTE: Using Gemini for Sentiment Analysis
# GEMINI_API_KEY must be set in environment
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

class NewsAnalyzer:
    def __init__(self):
        self.last_news = []
        self.sentiment_cache = "NEUTRAL"
        self.last_analysis_time = 0

    async def fetch_gold_news(self):
        """
        Fetches macro news that could impact Gold (XAU/USD).
        For production, this would use a real news API (like Finnhub, TreeNews, etc.)
        For now, we implement a robust skeleton.
        """
        # Placeholder for real news fetching logic
        # In a real scenario, we'd hit a streaming API or frequent polling
        headlines = [
            "Fed signals potential rate hike due to inflation",
            "Geopolitical tensions rise in the Middle East",
            "US Dollar strengthens against major currencies",
            "Central banks increase gold reserves"
        ]
        return headlines

    async def analyze_sentiment(self, headlines):
        """
        Uses Gemini to analyze if news are Bullish, Bearish or Neutral for Gold.
        """
        if not GEMINI_API_KEY:
            print("🚨 NewsAnalyzer: GEMINI_API_KEY not found. Defaulting to NEUTRAL.")
            return "NEUTRAL"

        if not headlines:
            return "NEUTRAL"

        prompt = f"""
        Analyze the following financial headlines and determine the overall sentiment for GOLD (XAU/USD).
        Respond ONLY with one word: BULLISH, BEARISH, or NEUTRAL.
        
        Headlines:
        {chr(10).join(headlines)}
        """

        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-pro:generateContent?key={GEMINI_API_KEY}"
        payload = {
            "contents": [{
                "parts": [{"text": prompt}]
            }]
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload) as resp:
                    result = await resp.json()
                    sentiment = result['candidates'][0]['content']['parts'][0]['text'].strip().upper()
                    if sentiment in ["BULLISH", "BEARISH", "NEUTRAL"]:
                        self.sentiment_cache = sentiment
                        return sentiment
        except Exception as e:
            print(f"🚨 NewsAnalyzer Error: {e}")
        
        return "NEUTRAL"

    async def get_gold_bias(self):
        """
        Returns the current trading bias.
        """
        now = datetime.now().timestamp()
        # Analyze news every 15 minutes or on demand
        if now - self.last_analysis_time > 900:
            news = await self.fetch_gold_news()
            self.sentiment_cache = await self.analyze_sentiment(news)
            self.last_analysis_time = now
        
        return self.sentiment_cache

if __name__ == "__main__":
    # Test
    async def test():
        analyzer = NewsAnalyzer()
        bias = await analyzer.get_gold_bias()
        print(f"Current Gold Bias: {bias}")
    
    asyncio.run(test())
