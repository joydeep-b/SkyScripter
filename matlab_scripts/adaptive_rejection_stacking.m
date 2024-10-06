% function stack = adaptive_rejection_stacking(dir_name, max_memory_gb)
  dir_name = '~/Astrophotography/2024-06-21/hd_201731/Light/.process/r_bkg*fit';
  rej = 0.5;
  max_memory_gb = 48;
  t_start = tic;
  dir_entries = dir(dir_name);
  N = length(dir_entries);
  % N = 10;
  % Compute the maximum block size that can be loaded.
  max_block_area = (max_memory_gb * 1024 * 1024 * 1024) / (8 * N);
  max_block_len = floor(sqrt(max_block_area));


  % Load the first image to get the dimensions.
  im = fitsread(sprintf('%s/%s', dir_entries(1).folder, dir_entries(1).name));
  [H, W] = size(im);

  fprintf('Stacking %d images of size %d x %d\n', N, H, W);
  fprintf('Max memory = %.3fGB, max block area = %d, max block len = %d\n', max_memory_gb, max_block_area, max_block_len);

  % Compute the tile size.
  tile_width = floor(W / ceil(W / max_block_len));
  tile_height = floor(H / ceil(H / max_block_len));
  num_tiles_x = ceil(W / tile_width);
  num_tiles_y = ceil(H / tile_height);
  fprintf('tile_width = %d, tile_height = %d, num_tiles_x = %d, num_tiles_y = %d\n', tile_width, tile_height, num_tiles_x, num_tiles_y);

  % tile = zeros(N, tile_height, tile_width);

  % First compute the median, max, min, and std of the images in the stack.
  % See if we can load the median values from a file.
  if exist('median_values.mat', 'file')
    fprintf('Loading median values from file... ');
    load('median_values.mat');
    fprintf('Done.\n');
  else
    median_values = zeros(N, 1);
    fprintf('Computing median values ');
    parfor i = 1:N
      filename = dir_entries(i).name;
      if mod(i, floor(N/20)) == 0
        fprintf('.');
      end
      % fprintf('Loading %s\n', filename);
      full_filename = sprintf('%s/%s', dir_entries(i).folder, filename);
      image_data = fitsread(full_filename);
      valid_pixels = image_data > 0;

      median_values(i) = median(reshape(image_data(valid_pixels), [], 1));
    end
    save('median_values.mat', 'median_values');
    fprintf(' Done.\n');
  end
  median_values = median_values(1:N);

  if 0
    fprintf('Initializing cache...\n');
    for tile_y = 1:num_tiles_y
      for tile_x = 1:num_tiles_x
        x0 = (tile_x - 1) * tile_width + 1;
        y0 = (tile_y - 1) * tile_height + 1;
        x1 = min(tile_x * tile_width, W);
        y1 = min(tile_y * tile_height, H);
        tile = load_sub_block(dir_name, x0, y0, x1, y1, N);
      end
    end
    fprintf('Done initializing cache.\n');
    return;
  end

  % Initialize the stack and the tile buffer.
  stack = zeros(H, W);
  rejections = zeros(H, W);
  for tile_y = 1:num_tiles_y
    for tile_x = 1:num_tiles_x
      fprintf('Processing tile (%d, %d)\n', tile_x, tile_y);
      x0 = (tile_x - 1) * tile_width + 1;
      y0 = (tile_y - 1) * tile_height + 1;
      x1 = min(tile_x * tile_width, W);
      y1 = min(tile_y * tile_height, H);
      this_tile_width = x1 - x0 + 1;
      this_tile_height = y1 - y0 + 1;

      tile = load_sub_block(dir_name, x0, y0, x1, y1, N);

      tile_stack = zeros(this_tile_height, this_tile_width);
      rejections_tile = zeros(this_tile_height, this_tile_width);
      parfor i = 1:this_tile_height
        for j = 1:this_tile_width
          pixel_sequence = reshape(tile(:, i, j), N, 1);
          filter = pixel_sequence > 0;
          pixel_sequence = pixel_sequence(filter);
          % pixel_sequence = pixel_sequence - median_values;
          % Reject outliers.
          m = median(filter);
          s = std(filter);
          filter = (pixel_sequence > m - rej * s) & (pixel_sequence < m + rej * s);
          pixel_sequence = pixel_sequence(filter);
          tile_stack(i, j) = mean(pixel_sequence);
          rejections_tile(i, j) = N - sum(filter);
        end
      end
      stack(y0:y1, x0:x1) = tile_stack;
      rejections(y0:y1, x0:x1) = rejections_tile;
    end
  end
  stack = (stack - min(stack(:))) / (max(stack(:)) - min(stack(:)));
  fitswrite(stack, '/Users/joydeepbiswas/Astrophotography/2024-06-21/hd_201731/.process/matlab_r_stack.fits');
  % imagesc(stretch_image(stack));
  imagesc(rejections);
  colorbar;
  axis image;
  fprintf('Done\n');
  toc(t_start);

% end