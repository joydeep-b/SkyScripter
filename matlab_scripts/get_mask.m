function mask = get_mask(input_image, clip_low, clip_high)
  margin_high = 0.1;
  margin_low = 0.01;
  if false
    mask_high = erfc((input_image - clip_high - 2.0 * margin_high) / margin_high) / 2;
    mask_low = (1 + erf((input_image - clip_low + 2.0 * margin_low) / margin_low)) / 2;
    mask = mask_low.* mask_high;
    % mask = mask_high;
    return
  end
  if false
    image_shifted = input_image - min(input_image(:));
    img_max = max(image_shifted(:));
    min_value = clip_low * img_max
    max_value = clip_high * img_max

    im = clip((input_image - min_value) / (max_value - min_value), 0, 1);
  else
    im = clip((input_image - clip_low) / clip_high, 0, 1);
  end

  % mask_high has a value of 0 where the image is greater than clip_high, and
  % scales to 1 where the image is less than clip_high - margin_high.
  mask_high = clip(im - (1 - margin_high), 0, 1);
  mask_high = mask_high / margin_high;
  mask_high = 1 - mask_high;


  mask_low = clip(im / margin_low, 0, 1);


  mask = mask_low.* mask_high;
  % mask = mask_low;
  mask = mask_high;

  conv_stddev = 3;
  kernel_size = 9 * conv_stddev;
  gaussian_kernel = fspecial('gaussian', kernel_size, conv_stddev);
  mask = conv2(double(mask), gaussian_kernel, 'same');
end