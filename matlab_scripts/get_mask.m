function mask = get_mask(input_image, clip_low, clip_high)
  margin_high = 0.1;
  margin_low = 0.01;
  mask = zeros(size(input_image));
  num_channels = size(input_image, 3);
  for i = 1:num_channels
    im_channel = clip((input_image(:,:,i) - clip_low) / (clip_high - clip_low), 0, 1);

    % mask_high has a value of 0 where the image is greater than clip_high, and
    % scales to 1 where the image is less than clip_high - margin_high.
    mask_high = clip(im_channel - (1 - margin_high), 0, 1);
    mask_high = mask_high / margin_high;
    mask_high = 1 - mask_high;


    mask_low = clip(im_channel / margin_low, 0, 1);


    mask(:,:,i) = mask_low.* mask_high;
    % mask = mask_low;
    % mask = mask_high;

    conv_stddev = 3;
    kernel_size = 9 * conv_stddev;
    gaussian_kernel = fspecial('gaussian', kernel_size, conv_stddev);
    mask(:,:,i) = conv2(double(mask(:,:,i)), gaussian_kernel, 'same');
  end
end