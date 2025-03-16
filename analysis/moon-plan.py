#!/usr/bin/env python3
import sys
import argparse
import datetime
import math
from astral import Observer
from astral.sun import dawn as get_dawn, dusk as get_dusk
import ephem
from geopy.geocoders import Nominatim
import pytz
from timezonefinder import TimezoneFinder

def get_geolocation(location_name):
    """Get latitude and longitude for a location using geopy."""
    geolocator = Nominatim(user_agent="astro_app")
    location = geolocator.geocode(location_name)
    if location is None:
        raise ValueError(f"Location '{location_name}' not found.")
    return location.latitude, location.longitude

def get_timezone(lat, lon):
    """Determine the timezone name based on latitude and longitude using TimezoneFinder."""
    tf = TimezoneFinder()
    tz_name = tf.timezone_at(lng=lon, lat=lat)
    if tz_name is None:
        # Fallback to UTC if timezone cannot be determined.
        tz_name = "UTC"
    return pytz.timezone(tz_name)

def max_moon_below_interval(observer, dusk_local, dawn_local, step_minutes=5):
    """
    Compute the maximum continuous interval between dusk and dawn (local time)
    during which the moon is below the horizon (altitude < 0).

    Returns a tuple: (max_duration (timedelta), best_start (datetime), best_end (datetime)).
    If no interval is found, returns (timedelta(0), None, None).
    """
    delta = datetime.timedelta(minutes=step_minutes)
    t = dusk_local
    current_interval_start = None
    max_duration = datetime.timedelta(0)
    best_start = None
    best_end = None

    while t <= dawn_local:
        # Set ephem observer's date using the UTC equivalent of local time t.
        observer.date = t.astimezone(pytz.UTC)
        moon = ephem.Moon(observer)
        alt_deg = math.degrees(moon.alt)

        if alt_deg < 0:
            if current_interval_start is None:
                current_interval_start = t
        else:
            if current_interval_start is not None:
                # The interval ends at the previous step.
                interval_end = t - delta
                interval_duration = interval_end - current_interval_start
                if interval_duration > max_duration:
                    max_duration = interval_duration
                    best_start = current_interval_start
                    best_end = interval_end
                current_interval_start = None
        t += delta

    # Check if an interval extends until dawn.
    if current_interval_start is not None:
        interval_duration = dawn_local - current_interval_start
        if interval_duration > max_duration:
            max_duration = interval_duration
            best_start = current_interval_start
            best_end = dawn_local

    return max_duration, best_start, best_end

def format_timedelta(td):
    """Format a timedelta object as HH:MM:SS."""
    total_seconds = int(td.total_seconds())
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

def main():
    parser = argparse.ArgumentParser(
        description="Compute nights when the moon is below the horizon for a continuous duration."
    )
    parser.add_argument("location", help="Location, e.g., 'Austin, TX'")
    parser.add_argument(
        "--start-date",
        type=str,
        default=datetime.date.today().isoformat(),
        help="Start date in YYYY-MM-DD format (default: today)"
    )
    parser.add_argument(
        "--num-days",
        type=int,
        default=30,
        help="Number of days to compute into the future (default: 30)"
    )
    
    args = parser.parse_args()
    
    # Parse the start date.
    start_date = datetime.datetime.strptime(args.start_date, "%Y-%m-%d").date()
    num_days = args.num_days

    # Get latitude and longitude.
    lat, lon = get_geolocation(args.location)
    
    # Determine local timezone based on location.
    tz = get_timezone(lat, lon)
    
    # Create an ephem observer for moon altitude calculations.
    moon_observer = ephem.Observer()
    moon_observer.lat = str(lat)
    moon_observer.lon = str(lon)
    moon_observer.horizon = '0'
    
    # Create an Astral Observer for twilight calculations.
    astral_observer = Observer(latitude=lat, longitude=lon)
    
    end_date = start_date + datetime.timedelta(days=num_days)
    current_date = start_date
    
    qualifying_nights = []
    total_duration = datetime.timedelta(0)
    
    while current_date < end_date:
        # Compute astronomical dusk for current_date and dawn for the next day
        # using depression=18 in the local timezone.
        dusk_local = get_dusk(astral_observer, date=current_date, tzinfo=tz, depression=18)
        dawn_local = get_dawn(astral_observer, date=current_date + datetime.timedelta(days=1), tzinfo=tz, depression=18)
    
        if dusk_local is None or dawn_local is None:
            current_date += datetime.timedelta(days=1)
            continue
        
        duration, best_start, best_end = max_moon_below_interval(moon_observer, dusk_local, dawn_local)
        
        # Only include nights with at least one hour (3600 seconds) duration.
        if duration.total_seconds() >= 3600:
            qualifying_nights.append((current_date, duration, best_start, best_end))
            total_duration += duration
        
        current_date += datetime.timedelta(days=1)
    
    if qualifying_nights:
        print("Nights with at least one hour of the moon below the horizon:")
        for d, duration, best_start, best_end in qualifying_nights:
            print(f"{d.isoformat()} - Duration: {format_timedelta(duration)} "
                  f"(Start: {best_start.strftime('%H:%M:%S')}, End: {best_end.strftime('%H:%M:%S')})")
        print("\nTotal duration over qualifying nights:", format_timedelta(total_duration))
    else:
        print("No nights found with the desired condition in the specified period.")

if __name__ == "__main__":
    main()
