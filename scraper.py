import requests
import random
import time
from config import URL, PROXY_API_URL, PROXY_CACHE_SECONDS, PROXY_ROTATION_STRATEGY

session = requests.Session()

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
    "Referer": "https://massyart.com/"
}

# Proxy rotation stat
_proxy_list = []
_proxy_index = 0
_proxy_last_fetch = 0
_failed_proxies = {}  # {proxy_url: failure_count}

MAX_PROXY_FAILURES = 3  # blacklist after 3 failures
FAILURE_TTL_SECONDS = 60  # remove from blacklist after 60 seconds


def fetch_proxies_from_api():
    """Fetch fresh proxies from ProxyScrape API.

    NOTE: SOCKS4/SOCKS5 proxies are FILTERED OUT - they don't work with requests library.
    Only HTTP/HTTPS proxies are kept.
    """
    global _proxy_list, _proxy_last_fetch

    try:
        response = requests.get(PROXY_API_URL, timeout=10)
        if response.status_code == 200:
            # Parse proxy list: format is "ip:port" per line
            proxies = [line.strip() for line in response.text.split('\n') if line.strip()]

            if proxies:
                # Convert to requests-compatible format if needed
                # FILTER OUT SOCKS4/SOCKS5 - they won't work
                formatted_proxies = []
                for proxy in proxies:
                    # Skip SOCKS proxies (they don't work with requests library)
                    if proxy.lower().startswith('socks4') or proxy.lower().startswith('socks5'):
                        continue

                    if not proxy.startswith('http'):
                        proxy = f"http://{proxy}"
                    formatted_proxies.append(proxy)

                _proxy_list = formatted_proxies[:10]  # Keep only 10 proxies (top of list)
                _proxy_last_fetch = time.time()
                print(f"PROXY: fetched {len(_proxy_list)} HTTP/HTTPS proxies from API (SOCKS excluded)")
                return True
        else:
            print(f"PROXY: API returned status {response.status_code}")
    except Exception as e:
        print(f"PROXY: API fetch failed: {e}")

    return False


def get_next_proxy():
    """Select next proxy from rotation (round-robin or random)."""
    global _proxy_index, _failed_proxies

    # Refresh proxy list if cache expired
    if time.time() - _proxy_last_fetch > PROXY_CACHE_SECONDS:
        fetch_proxies_from_api()

    # If no proxies, try to fetch
    if not _proxy_list:
        fetch_proxies_from_api()
        if not _proxy_list:
            return None

    # Clean up old failed proxies (TTL-based)
    now = time.time()
    expired = [p for p, (count, ts) in _failed_proxies.items()
               if now - ts > FAILURE_TTL_SECONDS]
    for p in expired:
        del _failed_proxies[p]

    # Filter out heavily failed proxies
    available = [p for p in _proxy_list
                 if p not in _failed_proxies or _failed_proxies[p][0] < MAX_PROXY_FAILURES]

    if not available:
        # All proxies exhausted, reset failures and try again
        _failed_proxies.clear()
        available = _proxy_list

    if not available:
        return None

    # Select proxy (round-robin or random)
    if PROXY_ROTATION_STRATEGY == "random":
        proxy = random.choice(available)
    else:  # round_robin (default)
        _proxy_index = _proxy_index % len(available)
        proxy = available[_proxy_index]
        _proxy_index += 1

    return proxy


def mark_proxy_failed(proxy):
    """Mark a proxy as failed (for temporary blacklisting)."""
    global _failed_proxies
    now = time.time()
    if proxy in _failed_proxies:
        count, _ = _failed_proxies[proxy]
        _failed_proxies[proxy] = (count + 1, now)
    else:
        _failed_proxies[proxy] = (1, now)


def fetch_page():
    """Fetch HTML page with proxy rotation and retry logic."""
    max_retries = 3
    retry_count = 0

    while retry_count < max_retries:
        proxy = get_next_proxy()

        if proxy is None:
            print("PROXY: no available proxies")
            return None

        try:
            proxies = {"http": proxy, "https": proxy}
            print(f"PROXY: fetching with {proxy}")

            r = session.get(
                URL,
                headers=headers,
                proxies=proxies,
                timeout=10
            )

            # Check for rate limit (403 Forbidden)
            if r.status_code == 403:
                print(f"PROXY: rate limited (403), trying next proxy")
                mark_proxy_failed(proxy)
                retry_count += 1
                continue

            # Success
            if r.status_code == 200:
                print(f"PROXY: success with {proxy}")
                return r.text

            # Other errors
            print(f"PROXY: HTTP {r.status_code}, trying next proxy")
            mark_proxy_failed(proxy)
            retry_count += 1
            continue

        except requests.exceptions.ProxyError as e:
            print(f"PROXY: proxy error ({proxy}): {e}")
            mark_proxy_failed(proxy)
            retry_count += 1

        except requests.exceptions.ConnectTimeout as e:
            print(f"PROXY: timeout ({proxy}): {e}")
            mark_proxy_failed(proxy)
            retry_count += 1

        except requests.exceptions.RequestException as e:
            print(f"PROXY: request error ({proxy}): {e}")
            mark_proxy_failed(proxy)
            retry_count += 1

    print("PROXY: all retries exhausted, returning None")
    return None
