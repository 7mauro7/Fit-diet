"""
Sincronizza le attività "svago" (cammino, bici, escursioni ecc.) da Garmin Connect
e le salva in svago-data.json, letto poi dalla pagina statica svago.html.

Eseguito da GitHub Actions (vedi .github/workflows/sync-svago.yml), non richiede
alcun server: le credenziali arrivano dai Secrets del repository.
"""
import os
import json
import sys
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
}


def main():
    if not GARMIN_EMAIL or not GARMIN_PASSWORD:
        print("GARMIN_EMAIL / GARMIN_PASSWORD non impostate, esco.")
        sys.exit(1)

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
            "embedUrl": f"https://connect.garmin.com/modern/activity/embed/{activity_id}" if activity_id else None,
        })

    result.sort(key=lambda a: a.get("startTimeLocal") or "", reverse=True)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump({"generatedAt": end.isoformat(), "activities": result}, f, ensure_ascii=False, indent=2)

    print(f"Salvate {len(result)} attività svago in {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
