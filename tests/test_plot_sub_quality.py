import numpy as np

from analysis import plot_sub_quality as psq


def test_robust_sigma_uses_mad():
    values = np.array([10.0, 11.0, 12.0, 1000.0])

    assert psq.robust_mad(values) == 1.0
    assert psq.robust_sigma(values) == 1.4826


def test_normalized_by_median_ignores_invalid_values():
    values = np.array([1.0, 2.0, np.nan, 4.0])

    normalized = psq.normalized_by_median(values)

    assert np.allclose(normalized[[0, 1, 3]], [0.5, 1.0, 2.0])
    assert np.isnan(normalized[2])


def test_parse_siril_star_count_uses_largest_channel_count():
    output = """
    Found 120 Gaussian profile stars in image, channel #0 (FWHM 4.2)
    Found 118 Gaussian profile stars in image, channel #1 (FWHM 4.3)
    """

    assert psq.parse_siril_star_count(output) == 120


def test_parse_siril_bgnoise_uses_stat_value():
    output = "Mean: 1.0, Median: 0.9, Sigma: 0.1, Min: 0, Max: 2, bgnoise: 0.01234"

    assert psq.parse_siril_bgnoise(output) == 0.01234


def test_siril_quality_is_star_count_over_bgnoise():
    feature = psq.FrameFeatures(index=0, star_count=50, bgnoise=2.5)

    assert psq.legacy_quality_score(feature.star_count, feature.bgnoise) == 20.0


def test_siril_quality_rejects_invalid_noise():
    assert np.isnan(psq.legacy_quality_score(50, 0.0))
