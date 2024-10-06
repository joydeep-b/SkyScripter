clear all;
if ~exist('D', 'var')
  dir_name = '~/Astrophotography/2024-06-21/hd_201731/Light/.process/r_bkg*fit';
  dir_entries = dir(dir_name);
  N = length(dir_entries);

  % x0 = 4152;
  % y0 = 2426;

  x0 = 3800;
  y0 = 2783;
  W = 1600;
  H = 1600;

  data_size_in_bytes = N * W * H * 4;
  % Size limit = 32GB.
  size_limit = 40 * 1024 * 1024 * 1024;
  if data_size_in_bytes > size_limit
    data_size_in_gb = data_size_in_bytes / (1024 * 1024 * 1024);
    error('load_sub_block:datatoolarge', 'Data size (%.3f) exceeds %.2fGB. Exiting.\n', data_size_in_gb, size_limit/(1024*1024*1024));
  end


  D = zeros(N, W, H);
  median_values = zeros(N, 1);
  % max_values = zeros(N, 1);
  % min_values = zeros(N, 1);
  % std_values = zeros(N, 1);
  for i = 1:N
    filename = dir_entries(i).name;
    % create a progress bar string of the form: [====>   ]
    n_total = 20;
    n_progress = round(i/N*n_total);
    progress = repmat('=', 1, n_progress);
    fprintf('Processing %4d/%4d [%s%s]\r', i, N, progress, repmat(' ', 1, n_total-n_progress));
    % fprintf('Loading %s\n', filename);
    full_filename = sprintf('%s/%s', dir_entries(i).folder, filename);
    image_data = fitsread(full_filename);
    valid_pixels = image_data > 0;

    median_values(i) = median(reshape(image_data(valid_pixels), [], 1));
    % max_values(i) = max(reshape(image_data(valid_pixels), [], 1));
    % min_values(i) = min(reshape(image_data(valid_pixels), [], 1));
    % std_values(i) = std(reshape(image_data(valid_pixels), [], 1));
    % max_deviation = max(max_values(i) - median_values(i), median_values(i) - min_values(i));
    D(i,:,:) = image_data(y0:y0+W-1, x0:x0+H-1) - median_values(i);
  end
end

figure(1);
std_median_values = std(median_values);
plot(median_values);
legend('median');

rej = 0.5;
% limit Y axis to 3 standard deviations.
ylim([median(median_values) - rej * std_median_values, median(median_values) + rej * std_median_values]);

% figure(3);
% plot([min_values, max_values, median_values, std_values]);
% legend('min', 'max', 'median', 'std');


figure(2);
rej = 1.5;
sub_sequence = reshape(D(:, 400, 400), N, 1);
plot(sub_sequence);
% Overlay the median value and +- 3 standard deviations.
hold on;
plot(1:N, repmat(median(sub_sequence), N, 1), 'r');
plot(1:N, repmat(median(sub_sequence) + rej*std(sub_sequence), N, 1), 'g');
plot(1:N, repmat(median(sub_sequence) - rej*std(sub_sequence), N, 1), 'g');
hold off;
% Count how many values are outside the +- rej standard deviations.
n_outliers = sum(sub_sequence > median(sub_sequence) + rej*std(sub_sequence) | sub_sequence < median(sub_sequence) - rej*std(sub_sequence));
fprintf('Number of outliers: %d (%.3f%%)\n', n_outliers, n_outliers/N*100);

% % Create an animation of all calibrated images
% figure(3);
% videoFile = VideoWriter('calibrated_images.MP4', 'MPEG-4');
% open(videoFile);
% for i = 1:N
%   imagesc(stretch_image(reshape(D(i, :, :), W, H)));
%   title(sprintf('Calibrated image %4d/%4d', i, N));
%   colorbar;
%   drawnow;
%   frame = getframe(gcf);
%   writeVideo(videoFile, frame);
%   pause(0.1);
% end
% close(videoFile);

% Compute the robust mean for each pixel.
fprintf('Computing robust mean and standard deviation...\n');
robust_mean = zeros(W, H);
% robust_std = zeros(W, H);
parfor i = 1:W
  for j = 1:H
    sub_sequence = reshape(D(:, i, j), N, 1);
    % Reject outliers.
    filter = sub_sequence < median(sub_sequence) + rej*std(sub_sequence) & sub_sequence > median(sub_sequence) - rej*std(sub_sequence);
    sub_sequence = sub_sequence(filter);
    robust_mean(i, j) = mean(sub_sequence);
    % robust_std(i, j) = std(sub_sequence);
  end
end
robust_mean = (robust_mean - min(robust_mean(:))) / (max(robust_mean(:)) - min(robust_mean(:)));
fitswrite(robust_mean, '/Users/joydeepbiswas/Astrophotography/2024-06-21/hd_201731/.process/test.fits');
fprintf('Done\n');

figure(4);
imagesc(stretch_image(robust_mean));