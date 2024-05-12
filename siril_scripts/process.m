process_dir = '/Users/joydeepbiswas/owncloud-system/Astrophotography/2024-03-28/.process';

all_files = dir(fullfile(process_dir, 'r_bkg_pp_light*.fit'));

N = length(all_files);

L = 512;

block_stack = zeros(L, L, 3, N);

W = 8191;
H = 5463;

X = 1143;
Y = 3366;

% X = 3782;
% Y = 2379;

i_start = H - Y - L/2;
i_end = H - Y + L/2 - 1;
j_start = X - L/2;
j_end = X + L/2 - 1;

for i = 1:N
  file = fullfile(process_dir, all_files(i).name);
  info = fitsinfo(file);
  fprintf('Processing %s\n', all_files(i).name);
  image = fitsread(file);
  imshow((image(i_start:i_end, j_start:j_end, :) - 0.01)*50)
  frame = getframe(gcf);
  writeVideo(v, frame);
  block_stack(:, :, :, i) = image(i_start:i_end, j_start:j_end, :);
end
scale_min = min(block_stack(:));
scale_max = max(block_stack(:));
block_stack = (block_stack - scale_min)/(scale_max - scale_min);

means = zeros(N, 1);
for i = 1:N
  image = block_stack(:, :, :, i);
  means(i) = median(image(:));
end

v = VideoWriter('blocks.MP4', 'MPEG-4');
v.FrameRate = 10;
open(v);
for i = 1:N
  image = block_stack(:, :, :, i);
  subplot(2, 1, 1)
  histogram(image(:), 0:0.001:1)
  text(0.16, 1.8e5, sprintf('Frame %d', i), 'Color', 'black', 'FontSize', 14)
  ylim([0 2e5])
  xlim([0 0.2])
  subplot(2, 1, 2)
  imagesc(image*15)
  axis image
  drawnow
  frame = getframe(gcf);
  writeVideo(v, frame);
end
close(v);

% Pick some random columns and plot the time series
M = 20;
D = zeros(N, M);
for i = 1:(M/2)
  x_rand = randi(L);
  y_rand = randi(L);
  D(:, i) = reshape(block_stack(x_rand, y_rand, 1, :), N, 1) - means;
end

for i = (M/2+1):M
  x_rand = 228 + randi(56);
  y_rand = 228 + randi(56);
  D(:, i) = reshape(block_stack(x_rand, y_rand, 2, :), N, 1) - means;
end
plot(D)

if false
  %% Median filter the image.
  image = fitsread(file);
  filtered_image = medfilt2(image, [100 100]);
end