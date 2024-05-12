process_dir = '/Users/joydeepbiswas/owncloud-system/Astrophotography/markarians_chain/.process';

all_files = dir(fullfile(process_dir, 'r_pp_light*.fit'));

N = length(all_files);

image_medians = zeros(N, 3);
for i = 1:N
  fprintf('Processing file %d of %d\n', i, N);
  file = fullfile(process_dir, all_files(i).name);
  info = fitsinfo(file);
  data = fitsread(file);
  R = data(:,:,1);
  G = data(:,:,2);
  B = data(:,:,3);
  image_medians(i, 1) = median(R(:));
  image_medians(i, 2) = median(G(:));
  image_medians(i, 3) = median(B(:));
end

plot(image_medians);