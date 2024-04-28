function [stretched_image, masks, raw_images] = process(working_dir)
  if ~exist('working_dir', 'var')
    working_dir = '../high_speed_bursts/totality_composite_01/.process';
    % working_dir = '../totality_brackets/totality_set_12';
  end
  
  N = length(dir(sprintf('%s/pp_light_0*.fit', working_dir)));
  infos = {};
  raw_images = {};
  uncal_images = {};
  maxes = zeros(N,3);
  exposure_times = zeros(N,1);
  for i = 1:N
    filename = sprintf('%s/pp_light_%05d.fit', working_dir, i);
    filename_uncal = sprintf('%s/light_%05d.fit', working_dir, i);
    % fprintf('Loading %s\n', filename);
    fprintf('Loading image %d\n', i);
    raw_images{i} = fitsread(filename);
    uncal_images{i} = fitsread(filename_uncal);
    infos{i} = fitsinfo(filename);
    for j = 1:length(infos{i}.PrimaryData.Keywords)
      if strcmp('EXPTIME', infos{i}.PrimaryData.Keywords{j,1})
        exposure_times(i) = infos{i}.PrimaryData.Keywords{j,2};
        break;
      end
    end
    % images{i} = images{i} / exposure_times(i);
    % fprintf('Image %d: Exposure time = %f\n', i, exposure_times(i));
    maxes(i,1) = max(reshape(raw_images{i}(:,:,1), [], 1));
    maxes(i,2) = max(reshape(raw_images{i}(:,:,2), [], 1));
    maxes(i,3) = max(reshape(raw_images{i}(:,:,3), [], 1));
  end

  % return

  mean_image = zeros(size(images{1}));
  mean_flux = raw_images{1} / exposure_times(1);
  max_clip = 0.8;
  max_maxes = max(maxes);
  r_clip = max_clip * max_maxes(1);
  g_clip = max_clip * max_maxes(2);
  b_clip = max_clip * max_maxes(3);
  fprintf('Clipping values: %f %f %f\n', r_clip, g_clip, b_clip);
  read_noise = 0.001;
  min_value = 0.001;
  conv_stddev = 9;
  kernel_size = 9 * conv_stddev;
  gaussian_kernel = fspecial('gaussian', kernel_size, conv_stddev);

  masks = {};
  total_mask = zeros(size(raw_images{1}(:,:,1)));
  for i = 1:N
    fprintf('Computing mask for image %d\n', i);
    if true
      mask_r = get_mask(raw_images{i}(:,:,1), min_value, r_clip);
      mask_g = get_mask(raw_images{i}(:,:,2), min_value, g_clip);
      mask_b = get_mask(raw_images{i}(:,:,3), min_value, b_clip);
      masks{i} = mask_r .* mask_g .* mask_b;
    else
      clipped_pixels = raw_images{i}(:,:,1) > r_clip    | ...
                      raw_images{i}(:,:,1) < min_value | ...
                      raw_images{i}(:,:,2) > g_clip    | ...
                      raw_images{i}(:,:,2) < min_value | ...
                      raw_images{i}(:,:,3) > b_clip    | ...
                      raw_images{i}(:,:,3) < min_value;
      % clipped_pixels = imerode(clipped_pixels, strel('disk', kernel_size));
      clipped_pixels = 1 - conv2(double(clipped_pixels), gaussian_kernel, 'same');
      % clipped_pixels = 1 - double(clipped_pixels);
      % imagesc(clipped_pixels); axis image;  colorbar; drawnow;
      masks{i} = clipped_pixels;
    end
    total_mask = total_mask + masks{i};
    % show(masks{i});
  end
  show(total_mask);
  % return

  % Open a log file.
  log_file = fopen('log.txt', 'w');
  fprintf(log_file, 'Start time: %s\n', datestr(now));
  fprintf(log_file, 'Number of images: %d\n', N);
  for j = 1:1
    fprintf('Iteration %d\n', j);
    mean_flux(mean_flux < 0) = 0;
    sum_variance = zeros(size(raw_images{1}));
    variances = {};
    fprintf('Variance  ');
    for i = 1:N
      fprintf('.');
      fprintf(log_file, 'Variance for image %d\n', i);
      % variance = mean_flux / exposure_times(i) + read_noise^2 / exposure_times(i)^2;
      variance = raw_images{i} / (exposure_times(i)^2) + read_noise^2 / exposure_times(i)^2;
      variances{i} = variance;
      if ~isreal(variance)
        fprintf('WARNING: Variance is not real for image %d\n', i);
      end
      sum_variance = sum_variance + 1./variance;
    end
    fprintf('\n');
    weights = {};
    fprintf('Weights   ');
    total_weight = zeros(size(raw_images{1}));
    for i = 1:N
      fprintf('.');
      fprintf(log_file, 'Weights for image %d\n', i);
      weights{i} = (1./variances{i}) ./ sum_variance;
      % weights{i} = ones(size(images{1}));
      weights{i}(:,:,1) = weights{i}(:,:,1) .* masks{i};
      weights{i}(:,:,2) = weights{i}(:,:,2) .* masks{i};
      weights{i}(:,:,3) = weights{i}(:,:,3) .* masks{i};
      if ~isreal(weights{i})
        fprintf('WARNING: Weights are not real for image %d\n', i);
      end
      total_weight = total_weight + weights{i};
    end
    fprintf('\n');
    mean_flux = zeros(size(raw_images{1}));
    fprintf('Mean Flux ');
    for i = 1:N
      fprintf('.');
      fprintf(log_file, 'Mean flux for image %d\n', i);
      mean_flux = mean_flux + weights{i} .* raw_images{i} / exposure_times(i);
      if ~isreal(mean_flux)
        fprintf('WARNING: Mean flux is not real after adding image %d\n', i);
      end
      % stretched_sub = double(locallapfilt(single(stretch_image(images{i})), 0.1, 0.01));
      % mean_flux = mean_flux + weights{i} .* stretched_sub;
    end
    fprintf('\n');
    mean_image = mean_flux ./ total_weight;
    mean_flux = mean_image;
    if ~isreal(mean_image)
      fprintf('WARNING: Mean image is not real after iteration %d\n', j);
    end
    % figure(1)
    % imagesc(mean_image); 
    % axis image;
    % drawnow;
    % fprintf('saving result.fit\n');
    % fitswrite(mean_image/max(mean_image(:)), 'result.fit');
    % Sleep for 1 second.
    % pause(1);
  end
  fclose(log_file);

  % Stretch the image.
  stretched_image = stretch_image(mean_image);

  output_filename = sprintf('%s/result.fit', working_dir);
  fprintf('saving %s\n', output_filename);
  fitswrite(stretched_image, output_filename);

  % return;

  figure(1)
  imagesc(stretched_image);
  axis image;

  figure(2)
  log_hist(stretched_image);

  figure(3)
  show(total_weight);
  axis image;

  return

  for i=1:N
    imshow(locallapfilt(single(stretch_image(images{i}/exposure_times(i))), 0.1, 0.01)); 
    fprintf('%d\n',i); 
    drawnow; 
    pause(1); 
  end

  imshow(locallapfilt(single(stretched_image), 0.1, 0.05))

  imshow(localtonemap(single(stretched_image), 'EnhanceContrast', 0.5))


  % Compute histograms for each image for each channel.
  hist_bins = [0:0.001:0.5];
  num_pixels = size(images{1}, 1) * size(images{1}, 2);
  % hists = zeros(length(hist_bins), N);
  hists = zeros(length(hist_bins), N, 3);
  means = zeros(N,1);
  for i = 1:N
    fprintf('Computing histogram for image %d\n', i);
    means(i) = mean(images{i}(:));
    % hists(:,i) = hist(raw_images{i}(:), hist_bins);
    % hists(:, i) = hist(reshape(raw_images{i}(:,:,1), [], 1), hist_bins);
    hists(:, i, 1) = hist(reshape(raw_images{i}(:,:,1), [], 1), hist_bins);
    hists(:, i, 2) = hist(reshape(raw_images{i}(:,:,2), [], 1), hist_bins);
    hists(:, i, 3) = hist(reshape(raw_images{i}(:,:,3), [], 1), hist_bins);
  end
  figure(2)
  plot(hist_bins, log(reshape(hists(:,:,1), [], N))/log(10), 'LineWidth', 2);
  figure(3)
  plot(hist_bins, log(reshape(hists(:,:,2), [], N))/log(10), 'LineWidth', 2);
  figure(4)
  plot(hist_bins, log(reshape(hists(:,:,3), [], N))/log(10), 'LineWidth', 2);

end

