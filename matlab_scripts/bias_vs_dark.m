bias = 65535.0 * fitsread('../../.tmp/master_bias_GAIN2_OFFSET3.fit');
dark = 65535.0 * fitsread('../../.tmp/master_dark_GAIN2.000_OFFSET3.000.fit');

hot_pixel_threshold = 3000;
cold_pixel_threshold = 400;

% Extract only the red channel from the bayer pattern.
bias = bias(1:2:end, 2:2:end);
dark = dark(1:2:end, 2:2:end);
% Convert to a column vector.
bias = bias(:);
dark = dark(:);
% Mask out hot and cold pixels.
% mask = bias > 3000 | dark > 3000 | bias < 500 | dark < 500;
mask = bias > hot_pixel_threshold | dark > hot_pixel_threshold | bias < cold_pixel_threshold | dark < cold_pixel_threshold;
% Count how many pixels are masked.
num_bad_pixels = length(find(mask));
fprintf('Masked %d pixels (%.3f%%).\n', num_bad_pixels, 100.0 * num_bad_pixels / length(mask));



bias(mask) = [];
dark(mask) = [];

figure(1);
mean_bias = mean(bias);
stddev_bias = std(bias);
bins = linspace(mean_bias - 5 * stddev_bias, mean_bias + 5 * stddev_bias, 200);
histogram(bias, bins);
title(sprintf('Bias Mean: %.3f, Stddev: %.3f', mean_bias, stddev_bias));

figure(2);
mean_dark = mean(dark);
stddev_dark = std(dark);
bins = linspace(mean_dark - 5 * stddev_dark, mean_dark + 5 * stddev_dark, 200);
histogram(dark, bins);
title(sprintf('Dark Mean: %.3f, Stddev: %.3f', mean_dark, stddev_dark));

delta = dark - bias;
figure(3);
mean_delta = mean(delta);
stddev_delta = std(delta);
bins = linspace(mean_delta - 5 * stddev_delta, mean_delta + 5 * stddev_delta, 200);
histogram(delta, bins);
% Print figure title
title(sprintf('Delta Mean: %.3f, Stddev: %.3f', mean_delta, stddev_delta));

