function xy = plot_colorspace(image)
  % Extract a 100x100 window from the center of the image.
  [rows, cols, ~] = size(image);
  center = [rows/2, cols/2];
  half_size = 50;
  window = image(rows/2-half_size:rows/2+half_size-1, cols/2-half_size:cols/2+half_size-1, :);
  % window_xyz = rgb2xyz(window,'WhitePoint','d50');
  window_xyz = rgb2xyz(window);
  window_xyz_column = reshape(window_xyz, [], 3);
  xyz_mag = sum(window_xyz_column, 2);
  x_primary = window_xyz_column(:,1) ./ xyz_mag;
  y_primary = window_xyz_column(:,2) ./ xyz_mag;
  wp = whitepoint('D65');
  wpMag = sum(wp, 2);
  x_wp = wp(:, 1)./wpMag;
  y_wp = wp(:, 2)./wpMag;
  figure(1);
  imshowpair(window, window_xyz, 'montage');
  figure(2);
  clf;
  plotChromaticity
  hold on
  xy = [x_primary, y_primary];
  scatter(x_primary, y_primary, 'marker', 'x', 'LineWidth', 1.5);
  hold off
end