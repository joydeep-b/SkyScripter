% MATLAB script to estimate camera sensor gain, read noise, and dark current.

% Gain  0 Offset 30 Mode 5
flats_dir = '~/Astrophotography/.tmp/calibration/characterization_flats';
% Gain  0 Offset 30 Mode 5
% flats_dir = '~/Astrophotography/.tmp/gain/Light/L/';
% Gain  0 Offset 30 Mode 4
% flats_dir = '~/Astrophotography/.tmp/gain/flats/';    
% Gain 26 Offset 30 Mode 4
% flats_dir = '~/Astrophotography/.tmp/gain2/Light/L/'; 

bias_dir = '~/Astrophotography/.tmp/bias/Bias/';

master_bias_file = '~/Astrophotography/.tmp/bias/Bias/.process/master_bias_MODEREADMODE_GAINGAIN_OFFSET3.fit';

master_bias = 65535.0 * fitsread(master_bias_file);



% =========================================================================
% Gain estimation.
% =========================================================================
flat_files = dir(fullfile(flats_dir, '*.fits'));
means = [];
vars = [];
colors = ['r', 'g', 'b', 'c', 'm', 'k'];
for j = 1:2:length(flat_files)
  flat1 = fitsread(fullfile(flats_dir, flat_files(j).name));
  flat2 = fitsread(fullfile(flats_dir, flat_files(j + 1).name));
  % flat1 = (flat1 - master_bias);
  % flat2 = (flat2 - master_bias);

  % Make sure the images are the same size.
  assert(all(size(flat1) == size(flat2)));

  W = size(flat1, 1);
  H = size(flat1, 2);
  box_size = 1000;
  X1 = floor((W - box_size) / 2);
  X2 = X1 + box_size;
  Y1 = floor((H - box_size) / 2);
  Y2 = Y1 + box_size;

  % flat1 = flat1(Y1:Y2, X1:X2);
  % flat2 = flat2(Y1:Y2, X1:X2);

  diff = flat2 - flat1;
  sum = flat1 + flat2;
  m = mean(sum(:));
  s = std(diff(:));
  fprintf('j: %02d Mean: %.3f, Variance: %.3f\n', j, m, s * s);
  means = [means m];
  vars = [vars s * s];
end
figure(1);
scatter(vars, means, 'x', 'LineWidth', 2);
p = polyfit(vars, means, 1);
x = linspace(min(vars), max(vars), 100);
y = polyval(p, x);
gain = p(1);
hold on;
xlabel('Variance');
ylabel('Mean');
plot(x, y, 'LineWidth', 1);
hold off;
fprintf('Gain: %fADU/e, Offset: %f\n', p(1), p(2));
fprintf('Gain: %fe/ADU, Offset: %f\n', 1/p(1), p(2));



% =========================================================================
% Read noise estimation.
% =========================================================================
% Let's do read noise estimation now.
bias_files = dir(fullfile(bias_dir, '*.fits'));
read_noise = zeros(length(bias_files), 1);
e = [];
for i = 1:length(bias_files)
  bias = fitsread(fullfile(bias_dir, bias_files(i).name));
  read_noise(i) = std(bias(:)-master_bias(:));
  e = [e, bias(1000, 1000)-master_bias(1000, 1000)];
end
mean_read_noise = mean(read_noise);
fprintf('Read noise: %f ADU +- %f\n\n', mean_read_noise, std(read_noise));
fprintf('Read noise: %f e +- %f\n', mean_read_noise * gain, std(read_noise) * gain);