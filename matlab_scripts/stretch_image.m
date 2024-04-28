function stretched_image = stretch_image(image)
  image = image - min(image(:)) + 0.001;
  stretched_image = log(1000*image);
  stretched_image = stretched_image - min(stretched_image(:));
  stretched_image = stretched_image / max(stretched_image(:));
end