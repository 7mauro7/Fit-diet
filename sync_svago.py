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
import xml.etree.ElementTree as ET
from datetime import date, timedelta

from garminconnect import Garmin

GARMIN_EMAIL = os.environ.get("GARMIN_EMAIL", "")
GARMIN_PASSWORD = os.environ.get("GARMIN_PASSWORD", "")
DAYS_BACK = int(os.environ.get("SVAGO_DAYS_BACK", "3650"))  # ~10 anni, praticamente "tutte"
OUTPUT_FILE = "svago-data.json"

SVAGO_TYPES = {
    "running", "walking", "cycling", "hiking", "trail_running",
    "mountain_biking", "road_biking", "track_running", "street_running",
    "walking_speed", "cross_country_skiing", "skate_skiing",
    "lap_swimming", "open_water_swimming",
}
# Attività cardio "vecchio stile", prima dell'inizio della scheda strutturata:
# niente percorso GPS da mostrare, sono sessioni indoor.
CARDIO_TYPES = {
    "cardio_training", "indoor_cardio", "elliptical", "fitness_equipment",
    "indoor_cycling", "treadmill_running",
}
CARDIO_CUTOFF_DATE = os.environ.get("CARDIO_CUTOFF_DATE", "2026-08-17")


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


def fetch_hr_detail(client, activity_id):
    """Recupera le zone di frequenza cardiaca e la serie temporale (per il grafico),
    stessa logica già usata per il cardio della palestra."""
    zones = []
    try:
        zone_data = client.get_activity_hr_in_timezones(activity_id)
        for z in zone_data or []:
            zones.append({"zone": z.get("zoneNumber"), "secs": z.get("secsInZone")})
    except Exception as e:
        print(f"Zone FC non disponibili per attività {activity_id}: {e}")

    hr_series = []
    try:
        details = client.get_activity_details(activity_id, maxchart=2000)
        descriptors = details.get("metricDescriptors") or []
        hr_index = None
        time_index = None
        for desc in descriptors:
            dkey = desc.get("key", "")
            if dkey == "directHeartRate":
                hr_index = desc.get("metricsIndex")
            elif dkey in ("directTimestamp", "sumElapsedDuration", "sumDuration") and time_index is None:
                time_index = desc.get("metricsIndex")
        metrics = details.get("activityDetailMetrics") or []
        if hr_index is not None:
            for m in metrics:
                vals = m.get("metrics") or []
                if len(vals) > hr_index and vals[hr_index] is not None:
                    t = vals[time_index] if (time_index is not None and len(vals) > time_index) else len(hr_series)
                    hr_series.append({"t": t, "hr": round(vals[hr_index])})
        if len(hr_series) > 150:
            step = len(hr_series) / 150
            hr_series = [hr_series[int(i * step)] for i in range(150)]
    except Exception as e:
        print(f"Serie FC dettagliata non disponibile per attività {activity_id}: {e}")

    return zones, hr_series


def fetch_gps_track(client, activity_id, max_points=150):
    """Scarica il tracciato GPS dell'attività (formato GPX, lo standard per le tracce)
    e lo riduce a un numero ragionevole di punti, per disegnarlo poi con OpenStreetMap
    senza dipendere dalla privacy dell'attività su Garmin Connect."""
    try:
        try:
            fmt = client.ActivityDownloadFormat.GPX
        except AttributeError:
            fmt = "gpx"
        gpx_bytes = client.download_activity(activity_id, dl_fmt=fmt)
        root = ET.fromstring(gpx_bytes)

        points = []
        for el in root.iter():
            if el.tag.endswith("trkpt"):
                lat = el.get("lat")
                lon = el.get("lon")
                if lat and lon:
                    points.append([round(float(lat), 5), round(float(lon), 5)])

        if len(points) > max_points:
            step = len(points) / max_points
            points = [points[int(i * step)] for i in range(max_points)]
        return points
    except Exception as e:
        print(f"Tracciato GPS non disponibile per attività {activity_id}: {e}")
        return []


def main():
    if not GARMIN_EMAIL or not GARMIN_PASSWORD:
        print("GARMIN_EMAIL / GARMIN_PASSWORD non impostate, esco.")
        sys.exit(1)

    # Riusa le posizioni e i tracciati già calcolati in run precedenti, per non
    # richiamare il servizio di geocodifica o riscaricare i GPX/dettagli FC ogni volta.
    location_cache = {}
    track_cache = {}
    hr_detail_cache = {}
    if os.path.exists(OUTPUT_FILE):
        try:
            with open(OUTPUT_FILE, encoding="utf-8") as f:
                prev = json.load(f)
            for a in prev.get("activities", []):
                if a.get("activityId") and a.get("location"):
                    location_cache[a["activityId"]] = a["location"]
                if a.get("activityId") and a.get("track"):
                    track_cache[a["activityId"]] = a["track"]
                if a.get("activityId") and (a.get("zones") or a.get("hrSeries")):
                    hr_detail_cache[a["activityId"]] = (a.get("zones") or [], a.get("hrSeries") or [])
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
        activity_date = (act.get("startTimeLocal") or "").split(" ")[0]

        is_svago = type_key in SVAGO_TYPES
        is_old_cardio = type_key in CARDIO_TYPES and activity_date and activity_date < CARDIO_CUTOFF_DATE
        if not is_svago and not is_old_cardio:
            continue
        category = "svago" if is_svago else "cardio"
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

        if category == "cardio":
            track = []  # sessioni indoor: nessun percorso GPS da mostrare
            if activity_id in hr_detail_cache:
                zones, hr_series = hr_detail_cache[activity_id]
            else:
                zones, hr_series = fetch_hr_detail(client, activity_id)
                hr_detail_cache[activity_id] = (zones, hr_series)
        else:
            zones, hr_series = [], []
            if activity_id in track_cache:
                track = track_cache[activity_id]
            else:
                track = fetch_gps_track(client, activity_id)
                track_cache[activity_id] = track

        result.append({
            "activityId": activity_id,
            "category": category,
            "type": type_key,
            "name": act.get("activityName"),
            "date": activity_date,
            "startTimeLocal": act.get("startTimeLocal"),
            "durationSec": act.get("duration"),
            "distanceMeters": act.get("distance"),
            "elevationGainM": act.get("elevationGain"),
            "calories": act.get("calories"),
            "avgHr": act.get("averageHR"),
            "maxHr": act.get("maxHR"),
            "location": location,
            "track": track,
            "zones": zones,
            "hrSeries": hr_series,
            "embedUrl": f"https://connect.garmin.com/modern/activity/embed/{activity_id}" if activity_id else None,
        })

    result.sort(key=lambda a: a.get("startTimeLocal") or "", reverse=True)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "generatedAt": end.isoformat(),
            "activities": result,
        }, f, ensure_ascii=False, indent=2)

    print(f"Salvate {len(result)} attività svago in {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
