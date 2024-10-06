function [sub_data] = load_sub_block(dir_name, x0, y0, x1, y1, max_N)
  if ~exist('dir_name', 'var') || ...
      ~exist('x0', 'var') || ~exist('y0', 'var') || ...
      ~exist('x1', 'var') || ~exist('y1', 'var')
      error('load_sub_block:argcheck', 'Usage: load_sub_block(dir_name, x0, y0, x1, y1)');
      return
  end

  % Try to see if we can load the sub block from the cache.
  if exist('.cache', 'dir')
    load('.cache/index.mat', 'cache_index');
    for i = 1:length(cache_index)
      if strcmp(cache_index(i).dir_name, dir_name) && ...
          cache_index(i).x0 == x0 && cache_index(i).y0 == y0 && ...
          cache_index(i).x1 == x1 && cache_index(i).y1 == y1
        cached_file = cache_index(i).filename;
        fprintf('Loading sub-block from cache: %s ', cached_file);
        load(cached_file, 'sub_data');
        % Ensure that the loaded data is of the correct size.
        [N, h, w] = size(sub_data);
        if h ~= y1 - y0 + 1 || w ~= x1 - x0 + 1
          error('load_sub_block:invalidcache', 'Cached data size (%d, %d) does not match requested size (%d, %d). Exiting.\n', h, w, y1 - y0 + 1, x1 - x0 + 1);
        end
        fprintf('Done.\n');
        return
      end
    end
  end

  dir_entries = dir(dir_name);
  N = length(dir_entries);
  if exist('max_N', 'var')
    N = min(N, max_N);
  end

  w = x1 - x0 + 1;
  h = y1 - y0 + 1;
  data_size_in_bytes = N * w * h * 4;
  % Size limit = 32GB.
  size_limit = 40 * 1024 * 1024 * 1024;
  if data_size_in_bytes > size_limit
    data_size_in_gb = data_size_in_bytes / (1024 * 1024 * 1024);
    error('load_sub_block:datatoolarge', 'Data size (%.3f) exceeds %.2fGB. Exiting.\n', data_size_in_gb, size_limit/(1024*1024*1024));
    return
  end


  sub_data = zeros(N, h, w);
  fprintf('Loading sub block (%d, %d, %d, %d) ', x0, y0, x1, y1);
  for i = 1:N
    filename = dir_entries(i).name;
    % create a progress bar string of the form: [====>   ]
    if mod(i, floor(N/20)) == 0
      fprintf('.');
    end
    % fprintf('Loading %s\n', filename);
    full_filename = sprintf('%s/%s', dir_entries(i).folder, filename);
    image_data = fitsread(full_filename);
    sub_data(i,:,:) = image_data(y0:y1, x0:x1);
  end
  fprintf(' Done.\n');

  % Save the sub block to the cache.
  if ~exist('.cache', 'dir')
    mkdir('.cache');
  end
  if ~exist('.cache/index.mat', 'file')
    cache_index = [];
  else
    load('.cache/index.mat', 'cache_index');
  end
  new_entry = struct('dir_name', dir_name, ...
                     'x0', x0, ...
                     'y0', y0, ...
                     'x1', x1, ...
                     'y1', y1, ...
                     'filename', sprintf('.cache/%d.mat', length(cache_index)));
  cache_index = [cache_index new_entry];
  save(cache_index(end).filename, 'sub_data',  '-v7.3', '-nocompression');
  save('.cache/index.mat', 'cache_index');
end