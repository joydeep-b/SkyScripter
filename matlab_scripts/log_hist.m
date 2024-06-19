function log_hist(image, increment)
  nchannels = size(image, 3);
  img_min = min(image(:));
  img_max = max(image(:));
  if nargin < 2
    increment = (img_max - img_min)/1000;
  end
  hist_bins = img_min:increment:img_max;
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

  % Make sure that the X axis labels are in integer format.
  % set(gca, 'XTickFormat', '%d');
  % XtickFormat(gca, '%d');
  xtickformat('%d');
end
