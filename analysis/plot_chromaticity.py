#!/usr/bin/env python3
import sys
import numpy as np
import matplotlib.pyplot as plt
import tifffile
import colour

# -----------------------------------------------------------------------------
# Check for command-line argument for the file path.
# -----------------------------------------------------------------------------
if len(sys.argv) < 2:
    sys.exit("Usage: {} <path_to_tiff_image>".format(sys.argv[0]))

image_path = sys.argv[1]

# -----------------------------------------------------------------------------
# User Parameters for intensity filtering.
# -----------------------------------------------------------------------------
lower_intensity = 0.3
upper_intensity = 0.99

# -----------------------------------------------------------------------------
# Step 1. Load the 32-bit float RGB TIFF image.
# -----------------------------------------------------------------------------
print("Loading image from '{}'...".format(image_path))
try:
    image = tifffile.imread(image_path)
    print("Image loaded successfully. Image shape: {}".format(image.shape))
except Exception as e:
    sys.exit("Error reading image {}: {}".format(image_path, e))

# -----------------------------------------------------------------------------
# Step 2. Extract Pixel Colors.
# -----------------------------------------------------------------------------
print("Extracting pixel colors from the image...")
# Reshape the image so that each row is an RGB triplet.
pixel_colors = image.reshape(-1, 3)
print("Found {} pixels.".format(len(pixel_colors)))

# -----------------------------------------------------------------------------
# Step 3. Convert Pixel Colors from RGB to CIE XYZ then to CIE xy.
# -----------------------------------------------------------------------------
print("Converting pixel colors from RGB to CIE XYZ and then to CIE xy...")
sRGB_cs = colour.RGB_COLOURSPACES["sRGB"]
XYZ = colour.RGB_to_XYZ(pixel_colors, sRGB_cs, sRGB_cs.whitepoint)
xy = colour.XYZ_to_xy(XYZ)
print("Color conversion completed.")

# -----------------------------------------------------------------------------
# Step 4. Filter by Intensity Range.
# -----------------------------------------------------------------------------
print("Filtering pixel colors by intensity (Y between {} and {})...".format(lower_intensity, upper_intensity))
Y = XYZ[:, 1]
mask = (Y >= lower_intensity) & (Y <= upper_intensity)
xy_filtered = xy[mask]
colors_filtered = pixel_colors[mask]
print("After filtering, {} pixels remain.".format(len(xy_filtered)))

# -----------------------------------------------------------------------------
# Step 5. Plot on a Custom CIE 1931 Chromaticity Diagram.
# -----------------------------------------------------------------------------
print("Preparing custom chromaticity diagram...")

# Create the figure and axis with a black background.
fig, ax = plt.subplots(figsize=(8, 8), facecolor="black")
ax.set_facecolor("black")

# Explicitly set the spines (axes lines) to white.
for spine in ax.spines.values():
    spine.set_edgecolor("white")
    spine.set_linewidth(1.5)

# Set the axis limits for the chromaticity diagram.
ax.set_xlim(0, 0.8)
ax.set_ylim(0, 0.9)

# Add explicit axis labels.
ax.set_xlabel("x", fontsize=14, color="white", labelpad=10)
ax.set_ylabel("y", fontsize=14, color="white", labelpad=10)

# Set the title to "CIE 1931 Chromaticity Diagram".
ax.set_title("CIE 1931 Chromaticity Diagram", fontsize=16, color="white", pad=15)

# Define tick locations and labels.
xticks = np.linspace(0, 0.8, 9)
yticks = np.linspace(0, 0.9, 10)
ax.set_xticks(xticks)
ax.set_yticks(yticks)
ax.set_xticklabels(["{:.1f}".format(x) for x in xticks], fontsize=12, color="white")
ax.set_yticklabels(["{:.1f}".format(y) for y in yticks], fontsize=12, color="white")
ax.tick_params(axis="both", colors="white")

# -----------------------------------------------------------------------------
# Plot the spectral locus as a colored line.
# -----------------------------------------------------------------------------
print("Computing spectral locus...")
wavelengths = np.linspace(380, 780, 400)
xy_spectral = []
colors_spectral = []

for w in wavelengths:
    XYZ_w = colour.wavelength_to_XYZ(w)
    xy_w = colour.XYZ_to_xy(XYZ_w)
    xy_spectral.append(xy_w)
    rgb_w = colour.XYZ_to_RGB(
        XYZ_w,
        sRGB_cs.whitepoint,
        sRGB_cs.whitepoint,
        sRGB_cs.matrix_XYZ_to_RGB,
    )
    rgb_w = np.clip(rgb_w, 0, 1)
    colors_spectral.append(rgb_w)
xy_spectral = np.array(xy_spectral)

for i in range(len(wavelengths) - 1):
    xseg = [xy_spectral[i, 0], xy_spectral[i + 1, 0]]
    yseg = [xy_spectral[i, 1], xy_spectral[i + 1, 1]]
    seg_color = (np.array(colors_spectral[i]) + np.array(colors_spectral[i + 1])) / 2
    ax.plot(xseg, yseg, color=seg_color, linewidth=2)

# Connect endpoints with a grey dashed line.
ax.plot(
    [xy_spectral[-1, 0], xy_spectral[0, 0]],
    [xy_spectral[-1, 1], xy_spectral[0, 1]],
    color="grey",
    linewidth=2,
    linestyle="--",
)

# -----------------------------------------------------------------------------
# Add tick markers along the spectral locus.
# -----------------------------------------------------------------------------
print("Adding tick markers along the spectral locus...")
tick_wavelengths = np.arange(380, 781, 20)
for w in tick_wavelengths:
    XYZ_w = colour.wavelength_to_XYZ(w)
    xy_w = colour.XYZ_to_xy(XYZ_w)
    ax.plot(xy_w[0], xy_w[1], marker='o', color='white', markersize=4)
    ax.text(
        xy_w[0] + 0.005, xy_w[1] + 0.005,
        "{} nm".format(int(w)),
        color="white",
        fontsize=8,
        verticalalignment="bottom",
        horizontalalignment="left"
    )

# -----------------------------------------------------------------------------
# Plot the sRGB triangle in red.
# -----------------------------------------------------------------------------
print("Plotting sRGB triangle...")
sRGB_triangle = sRGB_cs.primaries  # sRGB primaries in xy coordinates.
sRGB_triangle = np.vstack((sRGB_triangle, sRGB_triangle[0]))  # Close the triangle.
ax.plot(sRGB_triangle[:, 0], sRGB_triangle[:, 1], color='red', linewidth=2)

# -----------------------------------------------------------------------------
# Add the D65 white point.
# -----------------------------------------------------------------------------
D65_xy = colour.CCS_ILLUMINANTS["CIE 1931 2 Degree Standard Observer"]["D65"]
print("Plotting D65 point at xy =", D65_xy)
ax.plot(D65_xy[0], D65_xy[1], marker='o', color='red', markersize=8)
ax.text(
    D65_xy[0] + 0.01, D65_xy[1] + 0.01,
    "D65",
    color="red",
    fontsize=12,
    fontweight="bold",
    verticalalignment="bottom",
    horizontalalignment="left"
)

# -----------------------------------------------------------------------------
# Overlay the scatter plot of filtered pixel colors.
# -----------------------------------------------------------------------------
print("Overlaying scatter plot of filtered pixel colors...")
ax.scatter(
    xy_filtered[:, 0],
    xy_filtered[:, 1],
    c=colors_filtered,
    edgecolors="none",
    s=20,
    alpha=0.7,
)

# -----------------------------------------------------------------------------
# Add keyboard event handler to close the plot on Space or Escape.
# -----------------------------------------------------------------------------
def on_key(event):
    if event.key in [' ', 'escape']:
        plt.close()

fig.canvas.mpl_connect('key_press_event', on_key)

print("Displaying plot. Press Space or Escape to exit.")
plt.show()
print("Program finished.")
