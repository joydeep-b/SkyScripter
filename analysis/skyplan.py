#!/usr/bin/env python3
import argparse
import math
import sys
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import datetime, timedelta
from astropy.time import Time
import astropy.units as u
from astroplan import Observer, FixedTarget, download_IERS_A
from geopy.geocoders import Nominatim
from astropy.coordinates import get_body, get_sun
from timezonefinder import TimezoneFinder  # new import to get local timezone
import pytz
import warnings
from astropy.coordinates import NonRotationTransformationWarning
warnings.filterwarnings("ignore", category=NonRotationTransformationWarning)


# Download IERS data for accurate time/coordinate corrections.
download_IERS_A()

def get_observer(location_name):
    """
    Convert a location name to an astroplan Observer using geopy.
    Determines the timezone using the latitude and longitude.
    """
    geolocator = Nominatim(user_agent="astro_observer")
    location = geolocator.geocode(location_name)
    if location is None:
        raise ValueError(f"Could not geocode location: {location_name}")
    alt = location.altitude if location.altitude is not None else 0

    # Determine the timezone from the coordinates using TimezoneFinder.
    tf = TimezoneFinder()
    timezone_str = tf.timezone_at(lng=location.longitude, lat=location.latitude)
    if timezone_str is None:
        raise ValueError("Could not determine timezone for the provided location.")

    observer = Observer(latitude=location.latitude*u.deg,
                        longitude=location.longitude*u.deg,
                        elevation=alt*u.m,
                        name=location_name,
                        timezone=timezone_str)
    return observer

def get_twilight_times(observer, ref_time):
    """
    Given an observer and a reference time (preferably near local noon),
    compute the evening and following morning astronomical twilight.
    """
    evening_twilight = observer.twilight_evening_astronomical(ref_time, which='next')
    morning_twilight = observer.twilight_morning_astronomical(evening_twilight, which='next')
    return evening_twilight, morning_twilight

def compute_hours_above(altitudes, times, min_alt):
    """
    Estimate the number of hours during which the altitude is above min_alt.
    """
    above = altitudes > min_alt*u.deg
    total_duration = (times[-1] - times[0]).to(u.hour).value
    fraction = np.sum(above) / len(above)
    return fraction * total_duration

def moon_illumination(time, observer):
    """
    Compute the fraction of the Moon's disk illuminated at a given time 
    from the observer's location.
    This uses the approximation: phase_angle = π - (angular separation between Sun and Moon)
    and illumination fraction = (1 + cos(phase_angle)) / 2.
    """
    sun = get_sun(time)
    # Use get_body to retrieve the Moon's coordinates.
    moon = get_body('moon', time, observer.location)
    elongation = sun.separation(moon)  # Angular separation as seen from Earth.
    phase_angle = np.pi - elongation.to(u.rad).value
    illum = (1 + np.cos(phase_angle)) / 2.0
    return illum

def main():
    parser = argparse.ArgumentParser(
        description="Generate altitude graphs for an astronomical target (with Moon overlay) " +
                    "between astronomical dusk and dawn."
    )
    parser.add_argument("target", type=str,
                        help="Name of the astronomical object (e.g., M81)")
    parser.add_argument("--location", type=str, default="Brady, Texas",
                        help="Viewing location (e.g., 'Brady, Texas')")
    parser.add_argument("--n", type=int, default=9,
                        help="Number of days to generate graphs for (default: 9)")
    parser.add_argument("--min-alt", type=float, default=25,
                        help="Minimum altitude (in deg.) for counting visible hours (default: 25)")
    parser.add_argument("-d", "--start-date",
                        type=lambda s: datetime.strptime(s, "%Y-%m-%d").date(),
                        default=datetime.now().date(),
                        help="Start date (YYYY-MM-DD) for the first night (default: today)"
                        )

    args = parser.parse_args()

    # Create the observer from the provided location.
    try:
        observer = get_observer(args.location)
    except Exception as e:
        print(f"Error with location: {e}")
        return

    # Look up the target.
    try:
        target = FixedTarget.from_name(args.target)
    except Exception as e:
        print(f"Error looking up target '{args.target}': {e}")
        return

    cumulative_hours = 0
    days_info = []

    # Determine grid layout: choose ncols and nrows to best fill the screen.
    ncols = math.ceil(math.sqrt(args.n))
    nrows = math.ceil(args.n / ncols)
    fig, axes = plt.subplots(nrows, ncols, figsize=(5*ncols, 3*nrows), squeeze=False)
    axes = axes.flatten()
    def on_key(event):
        if event.key == 'escape':
            plt.close(fig)
            sys.exit(0)

    fig.canvas.mpl_connect('key_press_event', on_key)
    if isinstance(observer.timezone, str):
        local_tz = pytz.timezone(observer.timezone)
    else:
        local_tz = observer.timezone
    # Loop over the next n days.
    for i in range(args.n):
        # day_date = (datetime.now() + timedelta(days=i)).date()
        day_date = args.start_date + timedelta(days=i)
        ref_time = Time(f"{day_date} 12:00:00")
        try:
            evening_twilight, morning_twilight = get_twilight_times(observer, ref_time)
        except Exception as e:
            print(f"Error computing twilight times for {day_date}: {e}")
            continue


        # compute exact mid-point of astronomical night
        mid_astronomical = evening_twilight + (morning_twilight - evening_twilight) / 2
        mid_local = mid_astronomical.to_datetime(timezone=observer.timezone)
        
        # convert astro dusk/dawn to local datetimes
        evening_local = evening_twilight.to_datetime(timezone=observer.timezone)
        morning_local = morning_twilight.to_datetime(timezone=observer.timezone)

        # Create a time grid between evening and morning astronomical twilight.
        num_points = 200
        delta_sec = (morning_twilight - evening_twilight).sec
        times = evening_twilight + np.linspace(0, delta_sec, num_points)*u.second

        # Compute the altitude of the target at each time.
        altaz = observer.altaz(times, target)
        altitudes = altaz.alt

        # Compute Moon altitude for the same time grid.
        moon_altaz = observer.moon_altaz(times)
        moon_altitudes = moon_altaz.alt

        # Calculate the number of hours the target is above the given minimum altitude.
        hours_visible = compute_hours_above(altitudes, times, args.min_alt)
        cumulative_hours += hours_visible
        days_info.append((day_date, hours_visible))

        # Compute Moon illumination (phase) using the mid-time of the night.
        mid_time = times[len(times)//2]
        illum_fraction = moon_illumination(mid_time, observer)
        illum_percent = illum_fraction * 100

        # Convert times to local time for plotting using the observer's timezone.
        local_times = times.to_datetime(timezone=observer.timezone)
        # Plot the target altitude and overlay the Moon altitude.
        ax = axes[i]
        ax.plot(local_times, altitudes, label=f"{args.target} (Visible: {hours_visible:.2f} hrs)")
        ax.plot(local_times, moon_altitudes, label="Moon Altitude", linestyle='--')
        ax.axhline(args.min_alt, color='red', linestyle=':', label=f"Min Alt = {args.min_alt}°")
        # vertical line at middle of astro night
        ax.axvline(mid_local, linestyle=':', color='black')
        # grey vertical dotted lines at astro dusk and dawn
        ax.axvline(evening_local, linestyle=':', color='grey')
        ax.axvline(morning_local, linestyle=':', color='grey')
        
        ax.set_ylabel("Altitude (deg)")
        # Set Y-axis limits to 0-90 degrees.
        ax.set_ylim(-5, 90)
        ax.set_title(f"{day_date}\nMoon Illumination: {illum_percent:.0f}%")
        ax.legend()
        ax.grid(True)
        # Format the x-axis ticks to display only the hour.
        # ax.xaxis.set_major_formatter(mdates.DateFormatter('%H'))
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%H', tz=local_tz))
    

    # Print the summary of visible hours.
    print("\nVisibility Summary:")
    for day, hours in days_info:
        print(f"{day}: {hours:.2f} hours above {args.min_alt}°")
    print(f"\nCumulative visible hours over {args.n} day(s): {cumulative_hours:.2f} hours")
    
    # Turn off any unused subplots.
    for j in range(args.n, len(axes)):
        axes[j].axis('off')

    # Set the x-label on the bottom row of subplots.
    for ax in axes[-ncols:]:
        ax.set_xlabel("Local Time (Hour)")
    plt.tight_layout()
    plt.show()

if __name__ == "__main__":
    main()
