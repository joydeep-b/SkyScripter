% MATLAB script to estimate camera sensor gain, read noise, and dark current.

clear all;

% =========================================================================
% Gain estimation.
% =========================================================================

% Get list of flat field images.
flat_dirs = {'~/Astrophotography/.tmp/Flat/R'};
dark_data = [{'dark_current/master_dark_ISO400_FOCALLEN400_APERTURE8_EXP30.000.fit', 30.0}, 
{'dark_current/master_dark_ISO400_FOCALLEN400_APERTURE8_EXP119.900.fit', 119.9},
{'dark_current/master_dark_ISO400_FOCALLEN400_APERTURE8_EXP119.900_v2.fit', 119.9},
];
bias_dir = 'bias';
master_bias_file = 'master_bias_ISO400.fit';

bias = 2048;

colors = ['r', 'g', 'b', 'c', 'm', 'k'];
figure(1);
clf;
for j = 1:length(flat_dirs)
  curDir = flat_dirs(j);
  flatFiles = dir(fullfile(curDir{1}, '*.fit'));
  W = 32;
  X = 2700;
  Y = 4045;
  X1 = X - W/2;
  X2 = X + W/2;
  Y1 = Y - W/2;
  Y2 = Y + W/2;

  means = [];
  vars = [];

  % There should be an even number of files, and at least 2.
  if mod(length(flatFiles), 2) ~= 0 || length(flatFiles) < 2
    fprintf('Error: %s does not contain an even number of flat field images.\n', curDir{1});
    continue;
  end
  for i = 1:2:length(flatFiles)
      % fprintf('Processing %s\n', flatFiles(i).name);
      % Read image.
      flat1 = fitsread(fullfile(curDir{1}, flatFiles(i).name));
      flat2 = fitsread(fullfile(curDir{1}, flatFiles(i + 1).name));
      im1 = flat1(Y1:2:Y2, X1:2:X2);
      im2 = flat2(Y1:2:Y2, X1:2:X2);
      diff = im2 - im1;
      sum = 0.5 * (im1 + im2);
      m = mean(sum(:));
      s = std(diff(:));
      means = [means m];
      vars = [vars s * s];
  end
  scatter(means, vars, colors(j), 'x', 'LineWidth', 2);
  p = polyfit(vars, means, 1);
  y = linspace(min(vars), max(vars), 100);
  x = polyval(p, y);
  hold on;
  plot(x, y, colors(j), 'LineWidth', 1);
  mean_minus_bias = means - bias;
  best_gain = (mean_minus_bias*mean_minus_bias')/(vars*mean_minus_bias');
  fprintf('Dir: %16s, Gain: %f, Offset: %f, BestGain: %f\n', curDir{1}, p(1), p(2), best_gain);
  if j == 4
    iso_400_sensor_gain = best_gain;
  end
end
xlabel('Mean');
ylabel('Variance');
title('Variance vs. Mean');
legend({'ISO 400', '', 'ISO 800', '', 'ISO 1600', '', 'eISO 400', '', 'eISO 800', ''}, 'Location', 'NorthWest');
hold off;
fprintf('ISO 400 sensor gain: %f\n', iso_400_sensor_gain);


% =========================================================================
% Read noise estimation.
% =========================================================================
% Let's do read noise estimation now.
bias_files = dir(fullfile(bias_dir, '*.fit'));
master_bias = 65535.0 * fitsread(master_bias_file);
read_noise = zeros(length(bias_files), 1);
for i = 1:length(bias_files)
  bias = fitsread(fullfile(bias_dir, bias_files(i).name));
  read_noise(i) = std(bias(:)-master_bias(:));
end
mean_read_noise = mean(read_noise);
fprintf('Read noise stddev at ISO 400: %f e +- %f\n', mean_read_noise * iso_400_sensor_gain, std(read_noise) * iso_400_sensor_gain);

% =========================================================================
% Dark current.
% =========================================================================

for i = 1:length(dark_data)
  dark_file = dark_data{i, 1};
  duration = dark_data{i, 2};
  dark = 65535.0 * fitsread(dark_file);
  dark = dark - master_bias;
  % Suppress outliers.
  dark = dark(:);
  sigma = std(dark(:));
  dark = dark(abs(dark) < 4 * sigma);
  dark_current = std(dark(:))^2;
  % fprintf('Dark charge at ISO 400, duration %f: %f e\n', duration, dark_current * iso_400_sensor_gain);
  fprintf('Dark current at ISO 400: %f e/s\n', dark_current / duration * iso_400_sensor_gain);
end

% =========================================================================
% Another way to estimate dark current.
% =========================================================================
bias = 65535.0 * fitsread(master_bias_file);
dark = 65535.0 * fitsread('dark_current/master_dark_ISO400_FOCALLEN400_APERTURE8_EXP119.900_v2.fit');

% Extract only the red channel from the bayer pattern.
bias = bias(1:2:end, 2:2:end);
dark = dark(1:2:end, 2:2:end);
% Convert to a column vector.
bias = bias(:);
dark = dark(:);
% Mask out hot and cold pixels.
mask = bias > 3000 | dark > 3000 | bias < 500 | dark < 500;
% Count how many pixels are masked.
num_bad_pixels = length(find(mask));
fprintf('Masked %d pixels (%.3f%%) from master bias and master dark.\n', num_bad_pixels, 100.0 * num_bad_pixels / length(mask));
% Remove masked pixels.
bias(mask) = [];
dark(mask) = [];


mean_bias = mean(bias);
stddev_bias = std(bias);
% figure(1);
% bins = linspace(mean_bias - 5 * stddev_bias, mean_bias + 5 * stddev_bias, 200);
% histogram(bias, bins);
% title(sprintf('Bias Mean: %.3f, Stddev: %.3f', mean_bias, stddev_bias));
fprintf('Bias Mean: %.3f, Stddev: %.3f\n', mean_bias, stddev_bias);

figure(2);
mean_dark = mean(dark);
stddev_dark = std(dark);
bins = linspace(mean_dark - 5 * stddev_dark, mean_dark + 5 * stddev_dark, 200);
histogram(dark, bins);
title(sprintf('Dark Mean: %.3f, Stddev: %.3f', mean_dark, stddev_dark));
fprintf('Dark Mean: %.3f, Stddev: %.3f\n', mean_dark, stddev_dark);

delta = dark - bias;
figure(3);
mean_delta = mean(delta);
stddev_delta = std(delta);
bins = linspace(mean_delta - 5 * stddev_delta, mean_delta + 5 * stddev_delta, 200);
histogram(delta, bins);
% Print figure title
title(sprintf('Delta Mean: %.3f, Stddev: %.3f', mean_delta, stddev_delta));
fprintf('Delta Mean: %.3f, Stddev: %.3f\n', mean_delta, stddev_delta);

dark_current = stddev_delta^2 * iso_400_sensor_gain / 119.9;
fprintf('Dark current at ISO 400: %f e/s\n', dark_current);
