"""VK Lead Hunter — scans VK groups/walls for comments matching keywords."""
import asyncio
import logging
import re
from datetime import datetime, timedelta

import aiohttp

logger = logging.getLogger("hunter.vk")

VK_API = "https://api.vk.com/method"
VK_VERSION = "5.199"


async def _vk_call(session: aiohttp.ClientSession, method: str, token: str, **params):
    params.update({"access_token": token, "v": VK_VERSION})
    async with session.get(f"{VK_API}/{method}", params=params) as resp:
        data = await resp.json()
    if "error" in data:
        logger.warning("VK API error %s: %s", method, data["error"].get("error_msg", ""))
        return None
    return data.get("response")


async def scan_vk_wall(token: str, target: str, keywords: list[str],
                       hours_back: int = 24, max_posts: int = 100) -> list[dict]:
    """Scan a VK group/user wall for posts & comments containing keywords.

    target: group domain or -group_id (e.g. 'auto_msk' or '-12345')
    Returns list of found leads: {author_id, author_name, text, source_url, platform}
    """
    results = []
    kw_patterns = [re.compile(re.escape(kw), re.IGNORECASE) for kw in keywords if kw.strip()]
    if not kw_patterns:
        return results

    cutoff = datetime.now() - timedelta(hours=hours_back)
    cutoff_ts = int(cutoff.timestamp())

    async with aiohttp.ClientSession() as session:
        # Resolve owner_id
        if target.lstrip("-").isdigit():
            owner_id = int(target)
        else:
            info = await _vk_call(session, "groups.getById", token, group_id=target)
            if not info:
                info = await _vk_call(session, "utils.resolveScreenName", token, screen_name=target)
                if info and info.get("type") == "group":
                    owner_id = -info["object_id"]
                else:
                    logger.warning("Cannot resolve VK target: %s", target)
                    return results
            else:
                owner_id = -info[0]["id"] if isinstance(info, list) else -info["groups"][0]["id"]

        # Fetch wall posts
        wall = await _vk_call(session, "wall.get", token, owner_id=owner_id, count=max_posts)
        if not wall:
            return results

        posts = wall.get("items", [])
        for post in posts:
            if post.get("date", 0) < cutoff_ts:
                continue

            post_id = post["id"]
            post_text = post.get("text", "")
            post_url = f"https://vk.com/wall{owner_id}_{post_id}"

            # Check post text
            if any(p.search(post_text) for p in kw_patterns):
                results.append({
                    "author_id": str(post.get("from_id", "")),
                    "author_name": "",
                    "text": post_text[:500],
                    "source_url": post_url,
                    "platform": "vk",
                    "type": "post",
                })

            # Fetch comments for this post
            comments = await _vk_call(
                session, "wall.getComments", token,
                owner_id=owner_id, post_id=post_id, count=100, extended=1,
            )
            if not comments:
                continue

            profiles = {}
            for p in comments.get("profiles", []):
                profiles[p["id"]] = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
            for g in comments.get("groups", []):
                profiles[-g["id"]] = g.get("name", "")

            for c in comments.get("items", []):
                if c.get("date", 0) < cutoff_ts:
                    continue
                ctext = c.get("text", "")
                if any(p.search(ctext) for p in kw_patterns):
                    from_id = c.get("from_id", 0)
                    results.append({
                        "author_id": str(from_id),
                        "author_name": profiles.get(from_id, ""),
                        "text": ctext[:500],
                        "source_url": f"{post_url}?reply={c['id']}",
                        "platform": "vk",
                        "type": "comment",
                    })

            await asyncio.sleep(0.35)  # VK rate limit

    return results


CITY_COORDS = {
    "москва": (55.7558, 37.6173), "санкт-петербург": (59.9343, 30.3351),
    "новосибирск": (55.0084, 82.9357), "екатеринбург": (56.8389, 60.6057),
    "казань": (55.7887, 49.1221), "нижний новгород": (56.2965, 43.9361),
    "челябинск": (55.1644, 61.4368), "самара": (53.1959, 50.1002),
    "омск": (54.9885, 73.3242), "ростов-на-дону": (47.2357, 39.7015),
    "уфа": (54.7388, 55.9721), "красноярск": (56.0153, 92.8932),
    "воронеж": (51.6720, 39.1843), "пермь": (58.0105, 56.2502),
    "волгоград": (48.7080, 44.5133), "краснодар": (45.0355, 38.9753),
    "сочи": (43.5855, 39.7231), "тюмень": (57.1553, 65.5619),
    "тольятти": (53.5078, 49.4204), "ижевск": (56.8498, 53.2045),
}


async def scan_vk_search(token: str, keywords: list[str], hours_back: int = 24,
                         city: str = "") -> list[dict]:
    """Use VK newsfeed.search to find posts matching keywords globally.
    If city is provided, search is geo-filtered (50km radius)."""
    results = []
    cutoff = datetime.now() - timedelta(hours=hours_back)
    cutoff_ts = int(cutoff.timestamp())

    query = " | ".join(keywords)
    if not query.strip():
        return results

    extra_params = {}
    if city:
        city_lower = city.strip().lower()
        coords = CITY_COORDS.get(city_lower)
        if coords:
            extra_params["latitude"] = coords[0]
            extra_params["longitude"] = coords[1]

    async with aiohttp.ClientSession() as session:
        data = await _vk_call(
            session, "newsfeed.search", token,
            q=query, count=50, start_time=cutoff_ts, extended=1,
            **extra_params,
        )
        if not data:
            return results

        profiles = {}
        for p in data.get("profiles", []):
            profiles[p["id"]] = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
        for g in data.get("groups", []):
            profiles[-g["id"]] = g.get("name", "")

        for item in data.get("items", []):
            from_id = item.get("from_id", item.get("source_id", 0))
            post_id = item.get("id", item.get("post_id", 0))
            owner_id = item.get("owner_id", from_id)
            results.append({
                "author_id": str(from_id),
                "author_name": profiles.get(from_id, ""),
                "text": item.get("text", "")[:500],
                "source_url": f"https://vk.com/wall{owner_id}_{post_id}",
                "platform": "vk",
                "type": "search",
            })

    return results
