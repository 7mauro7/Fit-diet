"""
Sincronizza le attività "svago" (cammino, bici, corsa, nuoto, escursioni ecc.) da
Garmin Connect e le salva in svago-data.json, letto poi dalla pagina statica svago.html.

Eseguito da GitHub Actions (vedi .github/workflows/sync-svago.yml), non richiede
alcun server: le credenziali arrivano dai Secrets del repository.
"""
import os
import json
import re
import sys
import time
import urllib.request
import urllib.parse
from datetime import date, timedelta

from garminconnect import Garmin

GARMIN_EMAIL = os.environ.get("GARMIN_EMAIL", "")
GARMIN_PASSWORD = os.environ.get("GARMIN_PASSWORD", "")
DAYS_BACK = int(os.environ.get("SVAGO_DAYS_BACK", "60"))
RELIVE_PROFILE_USERNAME = os.environ.get("RELIVE_PROFILE_USERNAME", "7mauro7")
OUTPUT_FILE = "svago-data.json"

SVAGO_TYPES = {
    "running", "walking", "cycling", "hiking", "trail_running",
    "mountain_biking", "road_biking", "track_running", "street_running",
    "walking_speed", "cross_country_skiing", "skate_skiing",
    "lap_swimming", "open_water_swimming",
}


def reverse_geocode(lat, lon):
    """Ricava città/paese dalle coordinate GPS di partenza, via Nominatim (OpenStreetMap).
    Nessuna chiave richiesta, ma va usato con moderazione (max ~1 richiesta al secondo)."""
    try:
        url = "https://nominatim.openstreetmap.org/reverse?" + urllib.parse.urlencode({
            "format": "json", "lat": lat, "lon": lon, "zoom": 10, "accept-language": "it",
        })
        req = urllib.request.Request(url, headers={"User-Agent": "fit-diet-svago/1.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.load(r)
        addr = data.get("address", {})
        city = addr.get("city") or addr.get("town") or addr.get("village") or addr.get("municipality")
        country = addr.get("country")
        if city and country:
            return f"{city}, {country}"
        return city or country or None
    except Exception as e:
        print(f"Geocodifica fallita per {lat},{lon}: {e}")
        return None


def fetch_url(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; fit-diet-svago/1.0)"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode("utf-8", errors="ignore")


def fetch_relive_video_ids(username):
    """Legge la pagina pubblica del profilo Relive e ne estrae gli ID dei video
    più recenti mostrati lì (di solito gli ultimi ~20)."""
    try:
        html = fetch_url(f"https://www.relive.com/it/profile/{username}")
    except Exception as e:
        print(f"Impossibile leggere il profilo Relive: {e}")
        return []
    ids = re.findall(r"/(?:it/)?view/([A-Za-z0-9_-]+)", html)
    seen = set()
    ordered = []
    for i in ids:
        if i not in seen:
            seen.add(i)
            ordered.append(i)
    return ordered


def fetch_relive_video_date(video_id):
    """Apre la pagina di un singolo video Relive e prova a estrarne la data,
    cercando prima il formato JSON-LD/meta standard, poi una data ISO generica
    da qualche parte nella pagina."""
    try:
        html = fetch_url(f"https://www.relive.com/it/view/{video_id}")
    except Exception as e:
        print(f"Impossibile leggere il video Relive {video_id}: {e}")
        return None

    patterns = [
        r'"datePublished"\s*:\s*"(\d{4}-\d{2}-\d{2})',
        r'<meta[^>]+property=["\']article:published_time["\'][^>]+content=["\'](\d{4}-\d{2}-\d{2})',
        r'"startTime"\s*:\s*"(\d{4}-\d{2}-\d{2})',
        r'(\d{4}-\d{2}-\d{2})T\d{2}:\d{2}',  # una qualunque data-ora ISO nella pagina
    ]
    for pat in patterns:
        m = re.search(pat, html)
        if m:
            return m.group(1)
    return None


def match_relive_videos_to_activities(activities, relive_cache):
    """Abbina i video Relive alle attività Garmin in base alla data.
    Se più attività cadono nello stesso giorno, l'abbinamento è ambiguo e viene
    saltato (l'utente può sempre incollare il link a mano su quella attività)."""
    video_ids = fetch_relive_video_ids(RELIVE_PROFILE_USERNAME)
    by_date = {}
    for vid in video_ids:
        d = relive_cache.get(vid)
        if d is None:
            d = fetch_relive_video_date(vid)
            time.sleep(0.5)
        if d:
            relive_cache[vid] = d
            by_date.setdefault(d, []).append(vid)

    activities_by_date = {}
    for act in activities:
        activities_by_date.setdefault(act["date"], []).append(act)

    matched = 0
    for d, vids in by_date.items():
        acts_that_day = activities_by_date.get(d, [])
        if len(vids) == 1 and len(acts_that_day) == 1:
            acts_that_day[0]["reliveUrl"] = f"https://www.relive.com/it/view/{vids[0]}"
            matched += 1
    print(f"Relive: {len(video_ids)} video letti dal profilo, {matched} abbinati automaticamente per data")


def main():
    if not GARMIN_EMAIL or not GARMIN_PASSWORD:
        print("GARMIN_EMAIL / GARMIN_PASSWORD non impostate, esco.")
        sys.exit(1)

    # Riusa le posizioni già calcolate in run precedenti, per non richiamare
    # il servizio di geocodifica ogni volta per le stesse attività.
    location_cache = {}
    relive_date_cache = {}
    if os.path.exists(OUTPUT_FILE):
        try:
            with open(OUTPUT_FILE, encoding="utf-8") as f:
                prev = json.load(f)
            for a in prev.get("activities", []):
                if a.get("activityId") and a.get("location"):
                    location_cache[a["activityId"]] = a["location"]
            relive_date_cache = prev.get("reliveVideoDateCache", {})
        except Exception:
            pass

    client = Garmin(GARMIN_EMAIL, GARMIN_PASSWORD)
    client.login()

    end = date.today()
    start = end - timedelta(days=DAYS_BACK)

    activities = client.get_activities_by_date(start.isoformat(), end.isoformat())
    result = []
    for act in activities or []:
        type_key = ((act.get("activityType") or {}).get("typeKey") or "").lower()
        if type_key not in SVAGO_TYPES:
            continue
        activity_id = act.get("activityId")

        location = act.get("locationName")  # a volte Garmin lo fornisce già
        if not location and activity_id in location_cache:
            location = location_cache[activity_id]
        elif not location:
            lat = act.get("startLatitude")
            lon = act.get("startLongitude")
            if lat is not None and lon is not None:
                location = reverse_geocode(lat, lon)
                time.sleep(1)  # rispetta i limiti di Nominatim

        result.append({
            "activityId": activity_id,
            "type": type_key,
            "name": act.get("activityName"),
            "date": (act.get("startTimeLocal") or "").split(" ")[0],
            "startTimeLocal": act.get("startTimeLocal"),
            "durationSec": act.get("duration"),
            "distanceMeters": act.get("distance"),
            "elevationGainM": act.get("elevationGain"),
            "calories": act.get("calories"),
            "avgHr": act.get("averageHR"),
            "maxHr": act.get("maxHR"),
            "location": location,
            "embedUrl": f"https://connect.garmin.com/modern/activity/embed/{activity_id}" if activity_id else None,
        })

    result.sort(key=lambda a: a.get("startTimeLocal") or "", reverse=True)

    try:
        match_relive_videos_to_activities(result, relive_date_cache)
    except Exception as e:
        print(f"Abbinamento Relive fallito, si continua senza: {e}")

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "generatedAt": end.isoformat(),
            "activities": result,
            "reliveVideoDateCache": relive_date_cache,
        }, f, ensure_ascii=False, indent=2)

    print(f"Salvate {len(result)} attività svago in {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
