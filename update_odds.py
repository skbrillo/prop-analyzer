
import os
import json
import urllib.request
import urllib.error

API_KEY = os.environ.get("ODDS_API_KEY")

if not API_KEY:
    raise RuntimeError("ODDS_API_KEY is missing.")

url = "https://api.the-odds-api.com/v4/sports/?apiKey=" + API_KEY

request = urllib.request.Request(
    url,
    headers={"User-Agent": "PropAnalyzer/1.0"}
)

try:
    with urllib.request.urlopen(request, timeout=30) as response:
        data = json.load(response)
        print("API connection successful!")
        print("Sports returned:", len(data))
        print("NBA available:", any(
            sport.get("key") == "basketball_nba"
            for sport in data
        ))
        print("Remaining credits:",
              response.headers.get("x-requests-remaining", "Not reported"))
except urllib.error.HTTPError as error:
    print("API returned HTTP status:", error.code)
    raise
