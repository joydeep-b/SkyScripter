#!/usr/bin/env python3
import sys
import datetime
import webbrowser
import urllib.request
import warnings
import signal
import json
import time
from astropy.coordinates import SkyCoord


from PyQt5.QtCore import Qt, QDate, QSize, QThread, pyqtSignal
from PyQt5.QtGui import QIcon, QPixmap, QPainter, QColor, QFont, QImage
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QTableWidget, QTableWidgetItem, QTextEdit, QLabel, QDateEdit,
    QPushButton, QListWidget, QListWidgetItem, QSplitter, QTabWidget,
    QLineEdit, QGroupBox, QMessageBox, QToolBar, QAction, QDialog, 
    QButtonGroup, QRadioButton, QStackedWidget, QPlainTextEdit, 
    QFileDialog, QProgressDialog
)

from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
import numpy as np

# Astroplan and Astropy imports for real sky calculations
from astropy.time import Time
import astropy.units as u
from astroplan import Observer, FixedTarget, download_IERS_A
from astropy.coordinates import get_sun, get_body, AltAz
from timezonefinder import TimezoneFinder
from geopy.geocoders import Nominatim
import pytz
from astropy.coordinates import NonRotationTransformationWarning

# Suppress known Astropy warnings
warnings.filterwarnings("ignore", category=NonRotationTransformationWarning)

print("Downloading IERS data for accurate transformations...")
# Download IERS data once for accurate transformations
download_IERS_A()
print("IERS data downloaded successfully.")


def get_observer(location_name: str) -> Observer:
    """
    Convert a location name to an astroplan Observer using geopy.
    Determines the timezone from latitude/longitude via TimezoneFinder.
    """
    geolocator = Nominatim(user_agent="astro_observer")
    location = geolocator.geocode(location_name)
    if location is None:
        raise ValueError(f"Could not geocode location: {location_name}")
    alt = location.altitude if location.altitude is not None else 0

    tf = TimezoneFinder()
    timezone_str = tf.timezone_at(
        lng=location.longitude, lat=location.latitude
    )
    if timezone_str is None:
        raise ValueError("Could not determine timezone for provided location")

    observer = Observer(
        latitude=location.latitude * u.deg,
        longitude=location.longitude * u.deg,
        elevation=alt * u.m,
        name=location_name,
        timezone=timezone_str
    )
    return observer


def long_running_action(parent):
    """
    Shows a modal QProgressDialog while running a WorkerThread
    that emits progress signals.
    """
    dlg = QProgressDialog("Processing…", "Cancel", 0, 100, parent)
    dlg.setWindowModality(Qt.WindowModal)
    dlg.setAutoClose(True)
    dlg.setAutoReset(True)

    worker = WorkerThread(parent)
    worker.progress.connect(dlg.setValue)

    # If the user clicks “Cancel”, stop the worker
    dlg.canceled.connect(worker.terminate)

    worker.start()
    dlg.exec_()   # this blocks until dlg.close() or worker.quit()
    worker.wait() # ensure thread has finished


class WorkerThread(QThread):
    progress = pyqtSignal(int)   # emit percentage 0–100

    def run(self):
        total = 100
        for i in range(total + 1):
            time.sleep(0.05)       # simulate work
            self.progress.emit(i)  # send progress update

def find_date_at_altitude_at_dusk(observer: Observer,
                                target: FixedTarget,
                                desired_altitude_deg: float,
                                year: int) -> Time | None:
    """
    Return the UTC Time in the given `year` when `target` is rising through
    `desired_altitude_deg` at the evening astronomical dusk for `observer`.
    If no such date exists, return None.

    Parameters
    ----------
    observer : astroplan.Observer
        An Observer instance (with latitude, longitude, elevation set).
    target : astroplan.FixedTarget
        The FixedTarget (with a SkyCoord) you want to check.
    desired_altitude_deg : float
        The altitude (in degrees) you want the target to be at.
    year : int
        The calendar year in which to search.

    Returns
    -------
    astropy.time.Time or None
        A Time object (UTC) on which, at evening astronomical dusk, the target’s altitude
        equals `desired_altitude_deg` **while rising**.  Returns None if no root is found.
    """
    debug = False
    # 1) Create an array of Dates (one per day) for the entire year in UTC
    from calendar import isleap
    start_of_year = Time(f"{year}-01-01 00:00:00", scale="utc")
    # number of days in that year (accounting for leap years)
    days_in_year = 366 if isleap(year) else 365

    # Create a Time array, one per day at 00:00 UTC
    midnight_array = start_of_year + np.arange(days_in_year) * u.day
    
    altitudes = np.empty(days_in_year, dtype=float)
    midnight_array[0] = midnight_array[0].to_datetime(timezone=observer.timezone)
    dusk_time = observer.twilight_evening_astronomical(midnight_array[0], which="nearest")
    dusk_time = dusk_time.to_datetime(timezone=observer.timezone)
    altaz_frame = AltAz(obstime=dusk_time, location=observer.location)
    altitudes[0] = target.coord.transform_to(altaz_frame).alt.degree
    for i in range(days_in_year - 1):
        midnight_array[i+1] = midnight_array[i+1].to_datetime(timezone=observer.timezone)
        dusk_time = observer.twilight_evening_astronomical(midnight_array[i+1], which="nearest")
        dusk_time = dusk_time.to_datetime(timezone=observer.timezone)
        altaz_frame = AltAz(obstime=dusk_time, location=observer.location)
        altitudes[i+1] = target.coord.transform_to(altaz_frame).alt.degree - desired_altitude_deg
        if debug:
            print(f"Day {i+1}: {dusk_time} - Altitude: {altitudes[i+1]:.2f}°")
        if altitudes[i+1] > altitudes[i] and altitudes[i] * altitudes[i+1] < 0:
            # Just get the date of the crossing.
            if debug:
                print(f"Found crossing on day {i+1} at {dusk_time.date()}")
            return dusk_time.date()

    # If we reach here, no crossing was found. Return the date of the highest altitude.
    max_index = np.argmax(altitudes)
    return midnight_array[max_index].date()

def get_twilight_times(observer: Observer, ref_time: Time):
    """
    Given an observer and reference time (near local noon), compute the evening
    and following morning astronomical twilight times.
    """
    evening = observer.twilight_evening_astronomical(ref_time, which='next')
    morning = observer.twilight_morning_astronomical(evening, which='next')
    return evening, morning


def moon_illumination(time: Time, observer: Observer) -> float:
    """
    Compute fraction of Moon's disk illuminated at given time as seen by observer.
    """
    sun = get_sun(time)
    moon = get_body('moon', time, observer.location)
    elongation = sun.separation(moon)  # angular separation
    phase_angle = np.pi - elongation.to(u.rad).value
    return (1 + np.cos(phase_angle)) / 2.0


def compute_hours_above(altitudes, times, min_alt: float) -> float:
    """
    Estimate number of hours during which altitude > min_alt degrees.
    """
    above = altitudes > (min_alt * u.deg)
    total_duration = (times[-1] - times[0]).to(u.hour).value
    return np.sum(above) / len(above) * total_duration

def plot_altitude_on_axes(ax,
                          target_name: str,
                          target: FixedTarget,
                          date: datetime.date,
                          observer: Observer,
                          min_alt: float,
                          linewidth: float = 1.5,
                          show_legend: bool = True):
    """
    Draw altitude vs. time (and Moon) on the given axes `ax`.
    If show_legend=False, do not call ax.legend().
    """
    ax.clear()
    if observer is None:
        ax.text(0.5, 0.5, "Observer Error", ha='center', va='center')
        return

    # 2) Compute evening/morning astro twilight for `date`
    ref_time = Time(f"{date} 12:00:00")
    try:
        ev_twi, mo_twi = get_twilight_times(observer, ref_time)
    except Exception:
        ax.text(0.5, 0.5, "Twilight Error", ha='center', va='center')
        return

    # 3) Determine tzinfo
    if isinstance(observer.timezone, str):
        tz = pytz.timezone(observer.timezone)
    else:
        tz = observer.timezone

    # 4) Build a time grid between ev_twi and mo_twi
    delta_sec = (mo_twi - ev_twi).sec
    times = ev_twi + np.linspace(0, delta_sec, 200) * u.second  # 200 points

    # 5) Compute altitudes
    altaz = observer.altaz(times, target)
    altitudes = altaz.alt

    moon_altaz = observer.moon_altaz(times)
    moon_altitudes = moon_altaz.alt

    # 6) Compute visible hours above min_alt
    hours_visible = compute_hours_above(altitudes, times, min_alt)

    # 7) Convert to local datetime for plotting
    local_times = times.to_datetime(timezone=tz)

    # 8) Plot curves
    ax.plot(local_times, altitudes, linewidth=linewidth,
            label=f"{target_name} ({hours_visible:.1f}h >{min_alt}°)")
    ax.plot(local_times, moon_altitudes, linewidth=linewidth,
            linestyle="--", label="Moon Altitude")

    # 9) Mark horizontal/vertical lines
    ax.axhline(min_alt, color="red", linestyle=":", label=f"Min Alt={min_alt}°", linewidth=linewidth)
    evening_local = ev_twi.to_datetime(timezone=tz)
    morning_local = mo_twi.to_datetime(timezone=tz)
    mid_time = ev_twi + (mo_twi - ev_twi)/2
    mid_local = mid_time.to_datetime(timezone=tz)
    ax.axvline(evening_local, linestyle=":", color="grey", linewidth=linewidth)
    ax.axvline(morning_local, linestyle=":", color="grey", linewidth=linewidth)
    ax.axvline(mid_local, linestyle=":", color="black", linewidth=linewidth)

    ax.set_ylabel("Altitude (°)")
    ax.set_ylim(-10, 90)

    illum_frac = moon_illumination(mid_time, observer) * 100
    ax.set_title(f"{date}  Moon Illum.: {illum_frac:.0f}%")

    if show_legend:
        ax.legend(fontsize=8)
    ax.grid(True)
    ax.xaxis.set_major_formatter(
        __import__("matplotlib.dates").dates.DateFormatter('%H', tz=tz)
    )
    ax.set_xlabel("Local Time (Hour)")

class TargetEditorDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add New Target")
        self.resize(400, 300)
        layout = QVBoxLayout(self)

        # --- Name ---
        layout.addWidget(QLabel("Name:"))
        self.name_edit = QLineEdit()
        layout.addWidget(self.name_edit)

        # --- Coordinate Input Method ---
        coord_box = QGroupBox("Coordinates")
        cb_layout = QVBoxLayout(coord_box)
        self.manual_rb = QRadioButton("Manual RA/Dec")
        self.lookup_rb = QRadioButton("Lookup by Catalog Name")
        self.manual_rb.setChecked(True)
        cb_layout.addWidget(self.manual_rb)
        cb_layout.addWidget(self.lookup_rb)
        layout.addWidget(coord_box)

        # Stack: page 0 = manual, page 1 = lookup
        self.stack = QStackedWidget()
        # Page 0: manual RA/Dec
        manual_page = QWidget()
        m_layout = QHBoxLayout(manual_page)
        self.ra_edit = QLineEdit(); self.ra_edit.setPlaceholderText("e.g. 00h42m44.3s")
        self.dec_edit = QLineEdit(); self.dec_edit.setPlaceholderText("e.g. +41d16m9s")
        m_layout.addWidget(QLabel("RA:")); m_layout.addWidget(self.ra_edit)
        m_layout.addWidget(QLabel("Dec:")); m_layout.addWidget(self.dec_edit)
        # Page 1: catalog lookup
        lookup_page = QWidget()
        l_layout = QHBoxLayout(lookup_page)
        self.cat_edit = QLineEdit(); self.cat_edit.setPlaceholderText("e.g. M31")
        l_layout.addWidget(QLabel("Catalog Name:")); l_layout.addWidget(self.cat_edit)
        self.stack.addWidget(manual_page)
        self.stack.addWidget(lookup_page)
        layout.addWidget(self.stack)

        # Radio button logic
        bg = QButtonGroup(self)
        bg.addButton(self.manual_rb, 0)
        bg.addButton(self.lookup_rb, 1)
        bg.buttonClicked[int].connect(self.stack.setCurrentIndex)

        # --- Notes ---
        layout.addWidget(QLabel("Notes:"))
        self.notes_edit = QTextEdit()
        layout.addWidget(self.notes_edit)

        # --- Bookmarks ---
        layout.addWidget(QLabel("Bookmarks (one URL per line):"))
        self.bm_edit = QPlainTextEdit()
        layout.addWidget(self.bm_edit)

        # --- OK / Cancel ---
        btn_layout = QHBoxLayout()
        ok = QPushButton("OK"); cancel = QPushButton("Cancel")
        ok.clicked.connect(self.accept)
        cancel.clicked.connect(self.reject)
        btn_layout.addStretch(); btn_layout.addWidget(ok); btn_layout.addWidget(cancel)
        layout.addLayout(btn_layout)

    def get_data(self):
        """
        Returns a dict with:
          name: str
          fixed_target: astroplan.FixedTarget
          coord: SkyCoord
          notes: str
          bookmarks: [str]
        """
        name = self.name_edit.text().strip()
        notes = self.notes_edit.toPlainText().strip()
        bms = [line.strip() for line in self.bm_edit.toPlainText().splitlines() if line.strip()]

        # Determine coordinates
        if self.manual_rb.isChecked():
            ra = self.ra_edit.text().strip()
            dec = self.dec_edit.text().strip()
            coord = SkyCoord(ra, dec, unit=(u.hourangle, u.deg))
        else:
            catalog = self.cat_edit.text().strip()
            ft = FixedTarget.from_name(catalog)  # may raise if not found
            coord = ft.coord

        ft = FixedTarget(coord=coord, name=name)
        return {
            "name": name,
            "coord": coord,
            "fixed_target": ft,
            "notes": notes,
            "bookmarks": bms
        }


class DraggableListWidget(QListWidget):
    """
    QListWidget subclass that supports drag-and-drop reordering and deletion via Delete key.
    Calls change_callback when items change order or are deleted.
    """
    def __init__(self, change_callback=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.change_callback = change_callback
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setDragDropMode(QListWidget.InternalMove)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Delete:
            for item in self.selectedItems():
                self.takeItem(self.row(item))
            if self.change_callback:
                self.change_callback()
        else:
            super().keyPressEvent(event)

    def dropEvent(self, event):
        super().dropEvent(event)
        if self.change_callback:
            self.change_callback()


def create_placeholder_pixmap(text: str, color: QColor, size: QSize = QSize(100, 100)) -> QPixmap:
    """
    Create a simple colored placeholder pixmap with centered text.
    """
    pixmap = QPixmap(size)
    pixmap.fill(color)
    painter = QPainter(pixmap)
    painter.setPen(Qt.black)
    font = QFont("Arial", 12)
    painter.setFont(font)
    painter.drawText(pixmap.rect(), Qt.AlignCenter, text)
    painter.end()
    return pixmap


def load_pixmap_from_url(url: str, size: QSize = QSize(100, 100)) -> QPixmap:
    """
    Load an image from URL into QPixmap, faking a browser UA to avoid 403.
    If download fails, return a placeholder.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/114.0.0.0 Safari/537.36"
        )
    }
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req) as response:
            data = response.read()
            pixmap = QPixmap()
            pixmap.loadFromData(data)
            return pixmap.scaled(size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    except Exception:
        return create_placeholder_pixmap("ERR", QColor(255, 200, 200), size)


def generate_graph_pixmap(
    target_name: str,
    target: FixedTarget,
    date: datetime.date,
    observer: Observer,
    min_alt: float,
    size: QSize = QSize(100, 100)
) -> QPixmap:
    """
    Create a small (size×size) plot of altitude vs. time (with Moon overlay) for `date` at `observer`, but omit the legend.
    """

    fig = Figure(figsize=(size.width()/100, size.height()/100), dpi=100)
    ax = fig.add_axes([0, 0, 1, 1])

    # Pass show_legend=False here
    plot_altitude_on_axes(ax, target_name, target, date, observer, min_alt, show_legend=False, linewidth=4.0)

    canvas = FigureCanvasAgg(fig)
    canvas.draw()
    buf = np.asarray(canvas.buffer_rgba())
    h, w, _ = buf.shape

    image = QImage(buf.data, w, h, QImage.Format_RGBA8888)
    pixmap = QPixmap.fromImage(image)
    return pixmap.scaled(size, Qt.KeepAspectRatio, Qt.SmoothTransformation)

class RealAltitudeGraphCanvas(FigureCanvas):
    """
    A matplotlib canvas that plots actual altitude vs. time (with Moon overlay)
    for a given target name and date at a specified location.
    """
    def __init__(self, observer, parent=None, width=5, height=4, dpi=100):
        fig = Figure(figsize=(width, height), dpi=dpi)
        self.ax = fig.add_subplot(111)
        super().__init__(fig)
        if parent is not None:
            self.setParent(parent)
        self.observer = observer

    def plot_altitude(self, 
                      target_name: str, 
                      target: FixedTarget,
                      date: datetime.date, 
                      min_alt: float = 0.0):
        """
        Clear self.ax, then reuse plot_altitude_on_axes to draw onto that axes.
        """
        self.ax.clear()

        plot_altitude_on_axes(
            self.ax,
            target_name,
            target,
            date,
            self.observer,
            min_alt
        )
        self.figure.tight_layout()
        # Manually shrink margins: 0.05 from left/right, 0.05 from top/bottom
        self.figure.subplots_adjust(
            left=0.055,    # how close the left y-axis is to the left edge
            right=0.99,   # how close the right of the plot is to the right edge
            top=0.95,     # how close the top of the plot is to the top edge (leaves some room for title)
            bottom=0.08   # how close the x-axis is to the bottom edge
        )
        self.draw()


class ImagingPlannerGUI(QMainWindow):
    def __init__(self, location_name: str = "Brady, Texas", min_alt: float = 25.0):
        super().__init__()
        self.setWindowTitle("Astronomical Imaging Planner")
        self.showMaximized()
        
        # --- Toolbar for target management ---
        toolbar = QToolBar("Targets", self)
        self.addToolBar(toolbar)

        add_action = QAction("Add Target", self)
        toolbar.addAction(add_action)
        add_action.triggered.connect(self.add_target)
        
        # 2) Edit selected target
        edit_action = QAction("Edit Target", self)
        toolbar.addAction(edit_action)
        edit_action.triggered.connect(self.edit_target)

        # 3) Delete selected target
        delete_action = QAction("Delete Target", self)
        toolbar.addAction(delete_action)
        delete_action.triggered.connect(self.delete_target)

        # 4) Recalculate data for selected target
        recalc_action = QAction("Recalculate Target", self)
        toolbar.addAction(recalc_action)
        recalc_action.triggered.connect(self.recalculate_target)

        toolbar.addSeparator()  # group file‐ops together

        # 5) Save targets to file
        save_action = QAction("Save Targets", self)
        toolbar.addAction(save_action)
        save_action.triggered.connect(self.save_targets)

        # 6) Load targets from file
        load_action = QAction("Load Targets", self)
        toolbar.addAction(load_action)
        load_action.triggered.connect(self.load_targets)



        # Default location for all graphs
        self.location_name = location_name
        self.min_alt = min_alt
        
        print(f"Creating GUI")
        try:
            self.observer = get_observer(self.location_name)
        except Exception as e:
            QMessageBox.critical(self, "Observer Error", f"Could not create observer for {self.location_name}:\n{e}")
            sys.exit(1)
            self.observer = None

        # Main splitter divides left (table) and right (details) panes
        splitter = QSplitter(Qt.Horizontal)
        self.setCentralWidget(splitter)

        # ---------------- Left Pane: Target Table ----------------
        self.table = QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels([
            "Name",
            "Min Alt @ Astro Dusk",
            "Zenith @ Astro Midnight",
            "Peak Altitude (°)",
            "Image Thumb",
            "Graph Thumb"
        ])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)

        thumb_size = QSize(100, 100)
        self.table.setIconSize(thumb_size)
        self.table.verticalHeader().setDefaultSectionSize(thumb_size.height() + 10)
        self.table.setMinimumWidth(700)
        splitter.addWidget(self.table)

        # ---------------- Right Pane: Details ----------------
        right_splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(right_splitter)
        
        # --- Notes Section ---
        notes_container = QWidget()
        notes_layout = QVBoxLayout()
        notes_container.setLayout(notes_layout)
        coord_layout = QHBoxLayout()
        coord_layout.addWidget(QLabel("Coordinates:"))
        self.coord_label = QLabel("")            # initially empty
        coord_layout.addWidget(self.coord_label)
        coord_layout.addStretch()
        notes_layout.addLayout(coord_layout)
        notes_label = QLabel("Notes:")
        self.notes_edit = QTextEdit()
        notes_layout.addWidget(notes_label)
        notes_layout.addWidget(self.notes_edit)
        right_splitter.addWidget(notes_container)

        # --- Date & Graph Section (with its own vertical splitter) ---
        date_graph_splitter = QSplitter(Qt.Vertical)
        right_splitter.addWidget(date_graph_splitter)

        # Date selector section (with “<” and “>” buttons)
        date_container = QWidget()
        date_layout = QHBoxLayout()
        date_container.setLayout(date_layout)

        date_selector_label = QLabel("Select Custom Date:")
        self.date_edit = QDateEdit(calendarPopup=True)
        self.date_edit.setDate(QDate.currentDate())
        self.date_edit.setDisplayFormat("yyyy-MM-dd")

        # Create “<” and “>” buttons
        back_button = QPushButton("<")
        forward_button = QPushButton(">")

        date_layout.addWidget(date_selector_label)
        date_layout.addWidget(self.date_edit)
        date_layout.addWidget(back_button)
        date_layout.addWidget(forward_button)
        date_layout.addStretch()
        date_graph_splitter.addWidget(date_container)

        # Connect the buttons to date-stepping methods
        back_button.clicked.connect(self.decrement_date)
        forward_button.clicked.connect(self.increment_date)
        self.date_edit.dateChanged.connect(self.update_custom_graph)


        # Graphs group
        graphs_group = QGroupBox("Altitude vs. Time")
        graphs_layout = QVBoxLayout()
        graphs_group.setLayout(graphs_layout)
        date_graph_splitter.addWidget(graphs_group)

        self.graph_tabs = QTabWidget()
        graphs_layout.addWidget(self.graph_tabs)

        # Create four RealAltitudeGraphCanvas instances
        self.canvas_today = RealAltitudeGraphCanvas(self.observer, self, width=5, height=4)
        self.canvas_dusk = RealAltitudeGraphCanvas(self.observer, self, width=5, height=4)
        self.canvas_midnight = RealAltitudeGraphCanvas(self.observer, self, width=5, height=4)
        self.canvas_custom = RealAltitudeGraphCanvas(self.observer, self, width=5, height=4)

        self.graph_tabs.addTab(self.canvas_today, "Today")
        self.graph_tabs.addTab(self.canvas_dusk, "Min Alt @ Dusk")
        self.graph_tabs.addTab(self.canvas_midnight, "Zenith @ Midnight")
        self.graph_tabs.addTab(self.canvas_custom, "Custom Date")

        self.date_edit.dateChanged.connect(self.update_custom_graph)

        # --- Bookmark Links Section ---
        bookmarks_group = QGroupBox("Bookmark Links")
        bookmarks_layout = QVBoxLayout()
        bookmarks_group.setLayout(bookmarks_layout)
        right_splitter.addWidget(bookmarks_group)

        self.bookmark_list = DraggableListWidget(change_callback=self.on_bookmarks_changed)
        self.bookmark_list.setSelectionMode(QListWidget.SingleSelection)
        self.bookmark_list.itemActivated.connect(self.open_bookmark_url)
        self.bookmark_list.currentItemChanged.connect(self.on_bookmark_selection_changed)
        bookmarks_layout.addWidget(self.bookmark_list)

        self.bookmark_link_label = QLabel()
        self.bookmark_link_label.setOpenExternalLinks(True)
        bookmarks_layout.addWidget(self.bookmark_link_label)

        add_bookmark_layout = QHBoxLayout()
        self.bookmark_url_input = QLineEdit()
        self.bookmark_url_input.setPlaceholderText("Enter bookmark URL...")
        add_button = QPushButton("Add Bookmark")
        add_button.clicked.connect(self.add_bookmark_url)
        add_bookmark_layout.addWidget(self.bookmark_url_input)
        add_bookmark_layout.addWidget(add_button)
        bookmarks_layout.addLayout(add_bookmark_layout)

        # Set splitter stretch factors
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        right_splitter.setStretchFactor(0, 1)   # Notes
        right_splitter.setStretchFactor(1, 3)   # Date & Graphs
        right_splitter.setStretchFactor(2, 2)   # Bookmarks
        date_graph_splitter.setStretchFactor(0, 0)
        date_graph_splitter.setStretchFactor(1, 5)

        print(f"Done creating GUI")

        # ---------------- Data Initialization ----------------
        self.targets = []
        print(f"Loading targets...")
        self.load_data()
        self.populate_table()
        print(f"Done loading targets.")

        # Connect table selection changes
        self.table.selectionModel().selectionChanged.connect(self.on_table_selection_changed)

        # Select the first row by default
        if self.table.rowCount() > 0:
            self.table.selectRow(0)
            self.on_table_selection_changed()

    def add_target(self):
        dlg = TargetEditorDialog(self)
        if dlg.exec_() != QDialog.Accepted:
            return
        data = dlg.get_data()
        # Build your internal dict exactly as in load_data()
        new_entry = {
            "name":            data["name"],
            "coord":           data["coord"],
            "fixed_target":    data["fixed_target"],
            "min_dusk":        datetime.date.today(),       # placeholder
            "zenith_midnight": datetime.date.today(),       # placeholder
            "peak_alt":        0.0,                         # placeholder
            "bookmarks":       data["bookmarks"],
            "notes":           data["notes"]
        }
        # Append and refresh
        self.targets.append(new_entry)
        self.populate_table()

    def edit_target(self):
        """
        Open TargetEditorDialog pre-filled with the selected target’s data.
        On OK, update that entry in self.targets and refresh the table.
        """
        selected = self.table.selectedItems()
        if not selected:
            QMessageBox.warning(self, "Edit Target", "Please select a target to edit.")
            return
        row = selected[0].row()
        tgt = self.targets[row]

        # Launch dialog
        dlg = TargetEditorDialog(self)
        # Prefill Name
        dlg.name_edit.setText(tgt["name"])
        # Prefill coords in manual mode
        dlg.manual_rb.setChecked(True)
        dlg.stack.setCurrentIndex(0)
        ra_str  = tgt["coord"].ra.to_string(unit=u.hourangle, sep=':')
        dec_str = tgt["coord"].dec.to_string(unit=u.deg, sep=':')
        dlg.ra_edit.setText(ra_str)
        dlg.dec_edit.setText(dec_str)
        # Prefill notes
        dlg.notes_edit.setPlainText(tgt.get("notes", ""))
        # Prefill bookmarks
        dlg.bm_edit.setPlainText("\n".join(tgt.get("bookmarks", [])))

        if dlg.exec_() != QDialog.Accepted:
            return

        # Retrieve edited data
        data = dlg.get_data()
        # Update the target dict
        tgt["name"]         = data["name"]
        tgt["coord"]        = data["coord"]
        tgt["fixed_target"] = data["fixed_target"]
        tgt["notes"]        = data["notes"]
        tgt["bookmarks"]    = data["bookmarks"]
        # (leave min_dusk, zenith_midnight, peak_alt untouched)

        # Refresh table and keep the same row selected
        self.populate_table()
        self.table.selectRow(row)
        self.on_table_selection_changed()


    def delete_target(self):
        """
        Remove the selected target from self.targets (with confirmation),
        refresh the table, and clear the detail pane.
        """
        selected_items = self.table.selectedItems()
        if not selected_items:
            QMessageBox.warning(self, "Delete Target", "Please select a target to delete.")
            return

        row = selected_items[0].row()
        name = self.targets[row]["name"]

        reply = QMessageBox.question(
            self,
            "Confirm Delete",
            f"Are you sure you want to delete '{name}'?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return

        # Remove and repopulate
        self.targets.pop(row)
        self.populate_table()

        # Clear detail pane
        self.notes_edit.clear()
        self.coord_label.clear()
        self.bookmark_list.clear()
        self.bookmark_link_label.clear()
        self.canvas_today.ax.clear();    self.canvas_today.draw()
        self.canvas_dusk.ax.clear();     self.canvas_dusk.draw()
        self.canvas_midnight.ax.clear(); self.canvas_midnight.draw()
        self.canvas_custom.ax.clear();   self.canvas_custom.draw()

        # Select a valid row
        count = self.table.rowCount()
        if count > 0:
            new_row = min(row, count - 1)
            self.table.selectRow(new_row)
            self.on_table_selection_changed()


    def recalculate_target(self):
        selected = self.table.selectedItems()
        if not selected:
            QMessageBox.warning(self, "Recalculate Target", "Please select a target to recalculate.")
            return
        row = selected[0].row()
        tgt = self.targets[row]

        # Update the target dict
        tgt["name"]         = data["name"]
        tgt["coord"]        = data["coord"]
        tgt["fixed_target"] = data["fixed_target"]
        tgt["notes"]        = data["notes"]
        tgt["bookmarks"]    = data["bookmarks"]
        # (leave min_dusk, zenith_midnight, peak_alt untouched)

        # Refresh table and keep the same row selected
        self.populate_table()
        self.table.selectRow(row)
        self.on_table_selection_changed()

    def load_targets(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Targets", "targets.json",
            "JSON Files (*.json);;All Files (*)"
        )
        if not path:
            return
        self.load_data(path)

    def save_targets(self):
        """
        Prompt for a file name, then serialize self.targets (name, ra, dec, min_dusk,
        zenith_midnight, peak_alt, bookmarks, notes) to JSON.
        """
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Targets", "targets.json",
            "JSON Files (*.json);;All Files (*)"
        )
        if not path:
            return

        # Build a serializable list
        entries = []
        for tgt in self.targets:
            coord = tgt["coord"]
            ra_str  = coord.ra.to_string(unit=u.hourangle, sep=':')
            dec_str = coord.dec.to_string(unit=u.deg, sep=':')
            entries.append({
                "name":            tgt["name"],
                "ra":              ra_str,
                "dec":             dec_str,
                "min_dusk":        tgt["min_dusk"].strftime("%Y-%m-%d"),
                "zenith_midnight": tgt["zenith_midnight"].strftime("%Y-%m-%d"),
                "peak_alt":        tgt["peak_alt"],
                "bookmarks":       tgt.get("bookmarks", []),
                "notes":           tgt.get("notes", "")
            })

        try:
            with open(path, "w") as f:
                json.dump(entries, f, indent=2)
        except Exception as e:
            QMessageBox.critical(self, "Save Error", f"Failed to save:\n{e}")
            
    def decrement_date(self):
        """Move the custom date one day backward."""
        current_qdate = self.date_edit.date()  # QDate
        current_pydate = datetime.date(
            current_qdate.year(), current_qdate.month(), current_qdate.day()
        )
        new_pydate = current_pydate - datetime.timedelta(days=1)
        self.date_edit.setDate(QDate(new_pydate.year, new_pydate.month, new_pydate.day))
        # update_custom_graph will be triggered automatically by dateChanged

    def increment_date(self):
        """Move the custom date one day forward."""
        current_qdate = self.date_edit.date()
        current_pydate = datetime.date(
            current_qdate.year(), current_qdate.month(), current_qdate.day()
        )
        new_pydate = current_pydate + datetime.timedelta(days=1)
        self.date_edit.setDate(QDate(new_pydate.year, new_pydate.month, new_pydate.day))

    def load_data(self, path="targets.json"):
        try:
            entries = json.load(open(path, "r"))
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to load targets.json:\n{e}")
            self.targets = []
            return

        parsed_targets = []
        for entry in entries:
            try:
                # 1) parse coordinates
                coord = SkyCoord(entry["ra"], entry["dec"],
                                unit=(u.hourangle, u.deg))
                # 2) build FixedTarget
                fixed_tgt = FixedTarget(coord=coord,
                                        name=entry["name"])
                # 3) parse the rest
                parsed = {
                    "name":           entry["name"],
                    "coord":          coord,
                    "fixed_target":   fixed_tgt,
                    "min_dusk":       entry.get("min_dusk", None),
                    "zenith_midnight":datetime.datetime.strptime(
                                        entry["zenith_midnight"], "%Y-%m-%d"
                                    ).date(),
                    "peak_alt":       float(entry["peak_alt"]),
                    "bookmarks":      entry.get("bookmarks", []),
                    "notes":          entry.get("notes", "")
                }
                # long_running_action(parent=self)
                # 4) compute min_dusk if not provided
                if parsed["min_dusk"] is None or parsed["min_dusk"] == "":
                    # Find the date at which the target is at desired altitude at dusk
                    parsed["min_dusk"] = find_date_at_altitude_at_dusk(
                        self.observer, fixed_tgt, self.min_alt, datetime.date.today().year
                    )
                    if parsed["min_dusk"] is None:
                        raise ValueError(f"Could not find min_dusk for {entry['name']}")
                else:
                    # Parse existing min_dusk string into a date
                    parsed["min_dusk"] = datetime.datetime.strptime(
                        parsed["min_dusk"], "%Y-%m-%d"
                    ).date()

                parsed_targets.append(parsed)

            except Exception as e:
                print(f"Skipping entry {entry.get('name')}: {e}")
                continue

        self.targets = parsed_targets


    def populate_table(self):
        """
        Fill the table with each target’s data, generate thumbnails from the first image bookmark,
        and generate a simple graph thumbnail for display.
        """
        self.table.setRowCount(len(self.targets))
        thumb_size = QSize(300, 200)
        this_year = datetime.date.today().year
        print(f"Populating table with {len(self.targets)} targets...")
        for row, target in enumerate(self.targets):
            # Name & store data
            name_item = QTableWidgetItem(target["name"])
            name_item.setData(Qt.UserRole, target)
            self.table.setItem(row, 0, name_item)
            print(f"Adding target: {target['name']}")

            dusk_str = target["min_dusk"].strftime("%Y-%m-%d")
            zenith_str = target["zenith_midnight"].strftime("%Y-%m-%d")
            self.table.setItem(row, 1, QTableWidgetItem(dusk_str))
            self.table.setItem(row, 2, QTableWidgetItem(zenith_str))

            # Peak altitude
            peak_item = QTableWidgetItem(f"{target['peak_alt']:.1f}")
            self.table.setItem(row, 3, peak_item)

            # Thumbnail: first image-like bookmark
            first_image_url = None
            for url in target["bookmarks"]:
                if url.lower().endswith((".jpg", ".jpeg", ".png", ".gif")):
                    first_image_url = url
                    break
            if first_image_url:
                img_pixmap = load_pixmap_from_url(first_image_url, thumb_size)
            else:
                img_pixmap = create_placeholder_pixmap("IMG", QColor(200, 200, 255), thumb_size)

            # pick today’s date for the thumbnail (or keep target["min_dusk"] if you prefer)
            thumb_date = datetime.date.today()

            graph_pixmap = generate_graph_pixmap(
                target["name"],
                target["fixed_target"],
                thumb_date,
                self.observer,
                self.min_alt,
                thumb_size
            )

            img_item = QTableWidgetItem(QIcon(img_pixmap), "")
            graph_item = QTableWidgetItem(QIcon(graph_pixmap), "")
            img_item.setFlags(Qt.ItemIsEnabled)
            graph_item.setFlags(Qt.ItemIsEnabled)
            self.table.setItem(row, 4, img_item)
            self.table.setItem(row, 5, graph_item)

        self.table.resizeColumnsToContents()
        self.table.setColumnWidth(4, 120)
        self.table.setColumnWidth(5, 120)

    def on_table_selection_changed(self, selected=None, deselected=None):
        """
        When user selects a row, update notes, actual graphs (Today, Min Dusk, Zenith Midnight),
        and bookmark list.
        """
        selected_items = self.table.selectedItems()
        if not selected_items:
            return

        row = selected_items[0].row()
        target = self.table.item(row, 0).data(Qt.UserRole)
        name = target["name"]

        # Update notes
        self.notes_edit.setText(target.get("notes", ""))
        
        # Update coordinates display
        coord = target["coord"]   # this is your astropy.SkyCoord
        # show in "HH:MM:SS +DD:MM:SS" format
        ra_str  = coord.ra.to_string(unit=u.hour, sep=':')
        dec_str = coord.dec.to_string(unit=u.deg,  sep=':')
        self.coord_label.setText(f"{ra_str}  {dec_str}")
        print(f"Coordinates: {self.coord_label.text()}")


        # Plot real graphs for Today, min_dusk, zenith_midnight
        today_date = datetime.date.today()
        dusk_date = target["min_dusk"]
        zenith_date = target["zenith_midnight"]

        fixed_target = target["fixed_target"]
        self.canvas_today.plot_altitude(name, fixed_target, today_date, min_alt=self.min_alt)
        self.canvas_dusk.plot_altitude(name, fixed_target, dusk_date, min_alt=self.min_alt)
        self.canvas_midnight.plot_altitude(name, fixed_target, zenith_date, min_alt=self.min_alt)

        # Custom date tab: use current date in QDateEdit
        custom_q = self.date_edit.date()
        custom_date = datetime.date(custom_q.year(), custom_q.month(), custom_q.day())
        self.canvas_custom.plot_altitude(name, fixed_target, custom_date, min_alt= self.min_alt)

        # Update bookmark list
        self.bookmark_list.clear()
        for url in target.get("bookmarks", []):
            self.bookmark_list.addItem(QListWidgetItem(url))
        self.bookmark_link_label.clear()

    def update_custom_graph(self, qdate: QDate):
        """
        When the custom date changes, replot the Custom Date tab.
        """
        selected_items = self.table.selectedItems()
        if not selected_items:
            return
        row = selected_items[0].row()
        target = self.table.item(row, 0).data(Qt.UserRole)
        name = target["name"]
        peak_alt = target["peak_alt"]
        fixed_target = target["fixed_target"]
        custom_date = datetime.date(qdate.year(), qdate.month(), qdate.day())
        self.canvas_custom.plot_altitude(name, fixed_target, custom_date, min_alt=self.min_alt)

    def add_bookmark_url(self):
        """
        Add entered URL to the selected target’s bookmarks, update list and thumbnail.
        """
        url = self.bookmark_url_input.text().strip()
        if not url:
            QMessageBox.warning(self, "Invalid URL", "Please enter a valid URL.")
            return

        selected_items = self.table.selectedItems()
        if not selected_items:
            QMessageBox.warning(self, "No Target Selected", "Please select a target first.")
            return

        row = selected_items[0].row()
        target = self.table.item(row, 0).data(Qt.UserRole)

        # Append to bookmarks
        if "bookmarks" not in target:
            target["bookmarks"] = []
        target["bookmarks"].append(url)

        # Add to widget
        self.bookmark_list.addItem(QListWidgetItem(url))
        self.bookmark_url_input.clear()

        # Update thumbnail in table
        self.update_table_thumbnail(row)

    def on_bookmarks_changed(self):
        """
        Called when bookmarks are reordered or deleted. Sync back to target data and update thumbnail.
        """
        selected_items = self.table.selectedItems()
        if not selected_items:
            return
        row = selected_items[0].row()
        target = self.table.item(row, 0).data(Qt.UserRole)

        new_list = []
        for idx in range(self.bookmark_list.count()):
            new_list.append(self.bookmark_list.item(idx).text())
        target["bookmarks"] = new_list

        self.update_table_thumbnail(row)

    def update_table_thumbnail(self, row: int):
        """
        Regenerate thumbnail icon for target at given row, using first image-like bookmark.
        """
        target = self.table.item(row, 0).data(Qt.UserRole)
        thumb_size = QSize(100, 100)

        first_image_url = None
        for url in target.get("bookmarks", []):
            if url.lower().endswith((".jpg", ".jpeg", ".png", ".gif")):
                first_image_url = url
                break

        if first_image_url:
            img_pixmap = load_pixmap_from_url(first_image_url, thumb_size)
        else:
            img_pixmap = create_placeholder_pixmap("IMG", QColor(200, 200, 255), thumb_size)

        img_item = QTableWidgetItem(QIcon(img_pixmap), "")
        img_item.setFlags(Qt.ItemIsEnabled)
        self.table.setItem(row, 4, img_item)

    def on_bookmark_selection_changed(self, current: QListWidgetItem, previous: QListWidgetItem):
        """
        When a bookmark is selected, show it as a clickable link in the QLabel below.
        """
        if current:
            url = current.text()
            self.bookmark_link_label.setText(f'<a href="{url}">{url}</a>')
        else:
            self.bookmark_link_label.clear()

    def open_bookmark_url(self, item: QListWidgetItem):
        """
        Open the bookmark URL in the default web browser.
        """
        url = item.text()
        webbrowser.open(url)


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Launch GUI with observer settings")
    parser.add_argument(
        "--min-alt", type=float, default=25.0,
        help="Minimum altitude (in degrees) for counting visible hours (default: 25)"
    )
    parser.add_argument(
        "--location", type=str, default="Brady, Texas",
        help="Viewing location (e.g., 'Brady, Texas')"
    )
    args = parser.parse_args()

    app = QApplication(sys.argv)
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    window = ImagingPlannerGUI(
        location_name=args.location,
        min_alt=args.min_alt
    )
    window.showMaximized()
    sys.exit(app.exec_())

if __name__ == "__main__":
    main()
