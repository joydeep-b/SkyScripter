for i=1:12
  dirname = sprintf('../totality_brackets/totality_set_%02d', i);
  fprintf('\nProcessing %s\n', dirname);
  process(dirname);
end