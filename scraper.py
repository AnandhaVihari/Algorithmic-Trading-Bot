import requests
from config import URL

session = requests.Session()

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
    "Referer": "https://massyart.com/"
}

def fetch_page():

    r = session.get(
        URL,
        headers=headers,
        timeout=10
    )

    return r.text