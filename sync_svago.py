"""
Sincronizza le attività "svago" (cammino, bici, corsa, nuoto, escursioni ecc.) da
Garmin Connect e le salva in svago-data.json, letto poi dalla pagina statica svago.html.

Eseguito da GitHub Actions (vedi .github/workflows/sync-svago.yml), non richiede
alcun server: le credenziali arrivano dai Secrets del repository.
"""
import os
import json
import sys
import time
import urllib.request
import urllib.parse
from datetime import date, timedelta

from garminconnect import Garmin

GARMIN_EMAIL = os.environ.get("GARMIN_EMAIL", "")
GARMIN_PASSWORD = os.environ.get("GARMIN_PASSWORD", "")
DAYS_BACK = int(os.environ.get("SVAGO_DAYS_BACK", "60"))
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


def main():
    if not GARMIN_EMAIL or not GARMIN_PASSWORD:
        print("GARMIN_EMAIL / GARMIN_PASSWORD non impostate, esco.")
        sys.exit(1)

    # Riusa le posizioni già calcolate in run precedenti, per non richiamare
    # il servizio di geocodifica ogni volta per le stesse attività.
    location_cache = {}
    if os.path.exists(OUTPUT_FILE):
        try:
            with open(OUTPUT_FILE, encoding="utf-8") as f:
                prev = json.load(f)
            for a in prev.get("activities", []):
                if a.get("activityId") and a.get("location"):
                    location_cache[a["activityId"]] = a["location"]
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

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump({"generatedAt": end.isoformat(), "activities": result}, f, ensure_ascii=False, indent=2)

    print(f"Salvate {len(result)} attività svago in {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
