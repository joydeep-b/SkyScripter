function log_hist(image)
  nchannels = size(image, 3);
  img_min = min(image(:));
  img_max = max(image(:));
  hist_bins = img_min:(img_max - img_min)/1000:img_max;
  h = zeros(length(hist_bins), nchannels);
  colors = ['r', 'g', 'b'];
  for i = 1:nchannels
    img_channel = image(:, :, i);
    h(:, i) = hist(img_channel(:), hist_bins);
  end
  plot(hist_bins, log(h)/log(10), 'LineWidth', 2);
  if nchannels == 3
    legend('Red', 'Green', 'Blue');
  end

end
