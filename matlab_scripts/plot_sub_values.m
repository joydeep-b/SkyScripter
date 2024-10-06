% function stack = adaptive_rejection_stacking(dir_name, max_memory_gb)
dir_name = '~/Astrophotography/2024-06-21/hd_201731/Light/.process/r_bkg*fit';
rej = 1.5;
if ~exist('max_memory_gb', 'var')
  max_memory_gb = 32;
end
dir_entries = dir(dir_name);
N = length(dir_entries);
% N = 40;
% Compute the maximum block size that can be loaded.
max_block_area = (max_memory_gb * 1024 * 1024 * 1024) / (8 * N);
max_block_len = floor(sqrt(max_block_area));
fprintf('N = %d, max_memory_gb = %d, max_block_len = %d\n', N, max_memory_gb, max_block_len);

% Load the first image to get the dimensions.
im = fitsread(sprintf('%s/%s', dir_entries(1).folder, dir_entries(1).name));
[H, W] = size(im);

% Compute the tile size.
tile_width = floor(W / ceil(W / max_block_len));
tile_height = floor(H / ceil(H / max_block_len));
num_tiles_x = ceil(W / tile_width);
num_tiles_y = ceil(H / tile_height);
fprintf('tile_width = %d, tile_height = %d, num_tiles_x = %d, num_tiles_y = %d\n', tile_width, tile_height, num_tiles_x, num_tiles_y);

% Initialize the stack and the tile buffer.
stack = zeros(H, W);
% tile = zeros(N, tile_height, tile_width);

% First compute the median, max, min, and std of the images in the stack.
median_values = zeros(N, 1);
max_values = zeros(N, 1);
min_values = zeros(N, 1);
% std_values = zeros(N, 1);
stack = zeros(H, W);
for i = 1:N
  filename = dir_entries(i).name;
  if mod(i, floor(N/20)) == 0
    fprintf('.');
  end
  % fprintf('Loading %s\n', filename);
  full_filename = sprintf('%s/%s', dir_entries(i).folder, filename);
  image_data = fitsread(full_filename);
  stack = stack + image_data;
end
stack = stack / N;
fitswrite(stack, '/Users/joydeepbiswas/Astrophotography/2024-06-21/hd_201731/.process/matlab_r_stack2.fits');
