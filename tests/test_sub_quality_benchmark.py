from pathlib import Path

from astropy.io import fits
import numpy as np

from analysis.sub_quality_scoring import commands
from analysis.sub_quality_scoring import dataset
from analysis.sub_quality_scoring import discovery
from analysis.sub_quality_scoring import evaluation
from analysis.sub_quality_scoring import features
from analysis.sub_quality_scoring import formula_sweep
from analysis.sub_quality_scoring import metrics
from analysis.sub_quality_scoring import siril
from analysis.sub_quality_scoring.cli import parse_args
from analysis.sub_quality_scoring.metrics import sky_weighted_contrast
from analysis.sub_quality_scoring.metrics import star_yield
from analysis.sub_quality_scoring.metrics import stellar_quality


def make_record(left: Path, right: Path, winner: str) -> dataset.ComparisonRecord:
    return dataset.ComparisonRecord(
        timestamp="2026-05-30T12:00:00+00:00",
        group="test_group",
        left_path=left,
        right_path=right,
        winner=winner,
        left_preview=Path("left.png"),
        right_preview=Path("right.png"),
    )


def test_comparison_record_round_trips_through_json_dict(tmp_path):
    record = make_record(tmp_path / "a.fit", tmp_path / "b.fit", "left")
    record.note = "clearer stars"

    decoded = dataset.ComparisonRecord.from_json_dict(record.to_json_dict())

    assert decoded.version == dataset.DATASET_VERSION
    assert decoded.left_path == record.left_path
    assert decoded.right_path == record.right_path
    assert decoded.winner == "left"
    assert decoded.note == "clearer stars"


def test_write_comparisons_replaces_dataset_contents(tmp_path):
    dataset_path = tmp_path / "labels.jsonl"
    original = make_record(tmp_path / "a.fit", tmp_path / "b.fit", "left")
    replacement = make_record(tmp_path / "c.fit", tmp_path / "d.fit", "right")

    dataset.write_comparisons(dataset_path, [original])
    dataset.write_comparisons(dataset_path, [replacement])

    records = dataset.read_comparisons(dataset_path)
    assert len(records) == 1
    assert records[0].left_path == replacement.left_path
    assert records[0].winner == "right"


def test_generate_pairs_uses_sequential_paths_without_metric(tmp_path):
    paths = [tmp_path / f"sub_{index:03d}.fit" for index in range(4)]
    grouped = {"test_group": list(reversed(paths))}

    pairs = discovery.generate_pairs(
        grouped,
        mode="sequential",
        seed=0,
        max_pairs=None,
        skip_pairs={dataset.pair_key(paths[1], paths[2])},
    )

    assert [(pair.first_path, pair.second_path) for pair in pairs] == [
        (paths[0], paths[1]),
        (paths[2], paths[3]),
    ]


def test_cli_uses_new_metric_names_only():
    args = parse_args(["eval", "--metric", "stellar_quality"])

    assert args.dataset == dataset.DEFAULT_DATASET_PATH
    assert args.metric == "stellar_quality"
    assert "star_flux_count_background_v2" not in metrics.METRIC_NAMES


def test_score_command_defaults_to_recommended_metric():
    args = parse_args(["score", "markarians_calibrated/H"])

    assert args.metric == "stellar_quality"
    assert args.input_dirs == [Path("markarians_calibrated/H")]


def test_compare_metrics_command_defaults_to_formula_sweep():
    args = parse_args(["compare-metrics", "--top", "5"])

    assert args.top == 5
    assert args.sweep is True
    assert args.func == commands.run_compare_metrics


def test_metric_registry_exposes_descriptions_and_formulas():
    spec = metrics.get_metric("stellar_quality")

    assert spec.title == "Stellar Quality"
    assert "star_count^0.75" in spec.formula
    assert spec.description
    assert set(metrics.METRIC_NAMES) == {
        "star_yield",
        "stellar_contrast",
        "sky_weighted_contrast",
        "stellar_quality",
    }


def test_star_yield_parses_siril_output_and_scores():
    output = """
    Found 20 Gaussian profile stars in image, channel #0 (FWHM 3.1)
    Found 18 Gaussian profile stars in image, channel #1 (FWHM 3.3)
    bgnoise: 2.5
    """

    assert siril.parse_star_count(output) == 20
    assert siril.parse_bgnoise(output) == 2.5
    assert star_yield.score_from_values(20, 2.5) == 8.0
    assert np.isnan(star_yield.score_from_values(20, 0.0))


def test_siril_background_parser_uses_last_median():
    output = "Mean: 11.0, Median: 10.5, Sigma: 0.1, Min: 0, Max: 20, bgnoise: 0.25"

    assert siril.parse_background(output) == 10.5


def test_extract_star_background_features_returns_siril_scale_values(monkeypatch, tmp_path):
    image = np.full((5, 5), 10.0, dtype=np.float32)
    image[2, 2] = 20.0
    sub_path = tmp_path / "sub.fit"
    fits.writeto(sub_path, image)

    def fake_run_star_background_stats(_sub_path, _siril_path, _timeout):
        return [features.SirilStar(x=2.0, y=2.0, fwhm=0.1)], 10.0, 2.5

    monkeypatch.setattr(features, "run_star_background_stats", fake_run_star_background_stats)

    feature_values = features.extract_star_background_features(sub_path, "siril", 1.0)

    assert feature_values == {
        "star_count": 1,
        "median_mean_star_flux": 10.0,
        "background": 10.0,
        "bgnoise": 2.5,
    }


def test_run_star_background_stats_treats_missing_star_list_as_zero_stars(monkeypatch, tmp_path):
    sub_path = tmp_path / "saturated.fit"
    sub_path.write_bytes(b"fit")
    siril_output = "B&W layer: Mean: 63814.4, Median: 64377.9, Sigma: 1811.4, bgnoise: 230.4"

    def fake_run_siril_script(_script, _working_dir, _siril_path, _timeout, *, failure_context):
        # Emulate Siril finding no stars: the script succeeds but no .lst is written.
        return siril_output

    monkeypatch.setattr(features.siril, "run_siril_script", fake_run_siril_script)

    stars, background, bgnoise = features.run_star_background_stats(sub_path, "siril", 1.0)

    assert stars == []
    assert background == 64377.9
    assert bgnoise == 230.4


def test_extract_star_background_features_with_no_stars_yields_nan_score(monkeypatch, tmp_path):
    image = np.full((5, 5), 64377.0, dtype=np.float32)
    sub_path = tmp_path / "saturated.fit"
    fits.writeto(sub_path, image)

    def fake_run_star_background_stats(_sub_path, _siril_path, _timeout):
        return [], 64377.9, 230.4

    monkeypatch.setattr(features, "run_star_background_stats", fake_run_star_background_stats)

    feature_values = features.extract_star_background_features(sub_path, "siril", 1.0)

    assert feature_values["star_count"] == 0
    assert np.isnan(feature_values["median_mean_star_flux"])
    assert np.isnan(stellar_quality.score_from_features(feature_values))


def test_read_measurement_image_data_converts_xisf_with_siril(monkeypatch, tmp_path):
    sub_path = tmp_path / "sub.xisf"
    sub_path.write_bytes(b"xisf")
    converted_image = np.full((3, 3), 7.0, dtype=np.float32)
    scripts = []

    def fake_run_siril_script(script, working_dir, _siril_path, _timeout, *, failure_context):
        scripts.append((script, working_dir, failure_context))
        fits.writeto(working_dir / "measurement_input.fit", converted_image)
        return "converted"

    monkeypatch.setattr(features.siril, "run_siril_script", fake_run_siril_script)

    image = features.read_measurement_image_data(sub_path, "siril", 1.0)

    assert np.allclose(image, converted_image)
    assert len(scripts) == 1
    assert "setcpu 1" in scripts[0][0]
    assert f"load {features.siril.quote(str(sub_path))}" in scripts[0][0]
    assert "Siril XISF-to-FITS conversion" in scripts[0][2]


def test_measure_paths_invokes_on_result_for_each_completed(monkeypatch, tmp_path):
    sub_paths = []
    for index in range(3):
        sub_path = tmp_path / f"sub_{index}.fit"
        sub_path.write_bytes(b"fit")
        sub_paths.append(sub_path)

    monkeypatch.setattr(
        commands.metrics,
        "measure_metric",
        lambda _metric, path, _siril, _timeout: {"score": float(path.stem.split("_")[1])},
    )

    seen: list = []
    result = commands.measure_paths(
        sub_paths,
        "stellar_quality",
        "siril",
        1.0,
        1,
        on_result=lambda path, measurement: seen.append((path, measurement["score"])),
    )

    assert [path for path, _ in seen] == sub_paths
    assert [score for _, score in seen] == [0.0, 1.0, 2.0]
    assert set(result) == set(sub_paths)


def test_read_fits_image_data_flips_top_down_to_siril_orientation(tmp_path):
    image = np.full((6, 4), 100.0, dtype=np.float32)
    image[0, :] = 5000.0  # first stored row == top of a TOP-DOWN file
    sub_path = tmp_path / "td.fit"
    hdu = fits.PrimaryHDU(image)
    hdu.header["ROWORDER"] = "TOP-DOWN"
    hdu.writeto(sub_path)

    oriented = features.read_fits_image_data(sub_path)

    # Siril reports star Y from the bottom, so the bright top row must end up at
    # the bottom (last numpy row) after orienting.
    assert oriented[-1, 0] == 5000.0
    assert oriented[0, 0] == 100.0


def test_read_fits_image_data_keeps_bottom_up_orientation(tmp_path):
    image = np.arange(24, dtype=np.float32).reshape(6, 4)
    sub_path = tmp_path / "bu.fit"
    hdu = fits.PrimaryHDU(image)
    hdu.header["ROWORDER"] = "BOTTOM-UP"
    hdu.writeto(sub_path)

    oriented = features.read_fits_image_data(sub_path)

    assert np.array_equal(oriented, image)


def test_sky_weighted_contrast_scores_feature_dictionary():
    feature_values = {
        "star_count": 99,
        "median_mean_star_flux": 10.0,
        "background": 100.0,
        "bgnoise": 2.0,
    }

    score = sky_weighted_contrast.score_from_features(feature_values)

    assert np.isclose(score, np.log1p(99) * 10.0 / (2.0 * 10.0))


def test_stellar_quality_keeps_negative_flux_as_low_score():
    positive = {
        "star_count": 16,
        "median_mean_star_flux": 4.0,
        "background": 9.0,
        "bgnoise": 2.0,
    }
    negative = positive | {"median_mean_star_flux": -4.0}

    assert stellar_quality.score_from_features(positive) > 0.0
    assert stellar_quality.score_from_features(negative) < 0.0


def test_visualize_clear_filter_excludes_ties_and_rejects(tmp_path):
    records = [
        make_record(tmp_path / "a.fit", tmp_path / "b.fit", "left"),
        make_record(tmp_path / "c.fit", tmp_path / "d.fit", "tie"),
        make_record(tmp_path / "e.fit", tmp_path / "f.fit", "right"),
        make_record(tmp_path / "g.fit", tmp_path / "h.fit", "reject"),
    ]

    selected = dataset.filter_records_for_visualization(records, winners=dataset.CLEAR_WINNERS)

    assert [record.winner for record in selected] == ["left", "right"]


def test_parse_star_list_text_extracts_positions_and_mean_fwhm():
    star_list = """
    # star# layer B A beta X Y FWHMx [px] FWHMy [px]
    1 0 0.1 10.0 2.5 12.5 14.5 2.0 4.0 2.0 4.0 0.0 0.01 -1.0 0 Moffat
    malformed row
    2 0 0.1 9.0 2.5 20.0 25.0 3.0 3.0 3.0 3.0 0.0 0.01 -1.0 0 Moffat
    """

    stars = features.parse_star_list_text(star_list)

    assert stars == [
        features.SirilStar(x=12.5, y=14.5, fwhm=3.0),
        features.SirilStar(x=20.0, y=25.0, fwhm=3.0),
    ]


def test_median_star_mean_flux_uses_median_of_per_star_means():
    image = np.full((7, 7), 10.0, dtype=np.float32)
    image[1, 1] = 14.0
    image[5, 5] = 18.0
    stars = [
        features.SirilStar(x=1.0, y=1.0, fwhm=1.0),
        features.SirilStar(x=5.0, y=5.0, fwhm=1.0),
    ]

    flux = features.median_star_mean_flux(image, stars, 10.0, radius_scale=0.1)

    assert flux == 6.0


def test_star_aperture_slice_mask_uses_local_window():
    aperture = features.star_aperture_slice_mask(
        (100, 100),
        features.SirilStar(x=50.0, y=60.0, fwhm=2.0),
        radius_scale=1.5,
    )

    assert aperture is not None
    slices, local_mask = aperture
    assert slices == (slice(57, 64), slice(47, 54))
    assert local_mask.shape == (7, 7)
    assert local_mask.any()


def test_image_data_to_siril_stat_scale_converts_normalized_float_data():
    image = np.array([0.0, 0.5, 1.0], dtype=np.float32)

    scaled = features.image_data_to_siril_stat_scale(image)

    assert np.allclose(scaled, [0.0, 32767.5, 65535.0])


def test_formula_sweep_score_handles_exponents():
    feature_values = {
        "star_count": 16,
        "median_mean_star_flux": 8.0,
        "background": 4.0,
        "bgnoise": 2.0,
    }

    score = formula_sweep.formula_sweep_score(
        feature_values,
        star_exponent=0.5,
        flux_exponent=1.0,
        bgnoise_exponent=1.0,
        background_exponent=0.5,
    )

    assert score == 8.0


def test_formula_sweep_candidates_include_named_feature_terms():
    names = [candidate[0] for candidate in formula_sweep.formula_sweep_candidates()]

    assert any("log1p(star_count)" in name for name in names)
    assert any("star_count^0.75*signed_flux^0.75" in name for name in names)


def test_evaluate_pairwise_scores_counts_accuracy_ties_rejects_and_violations(tmp_path):
    records = [
        make_record(tmp_path / "a.fit", tmp_path / "b.fit", "left"),
        make_record(tmp_path / "c.fit", tmp_path / "d.fit", "right"),
        make_record(tmp_path / "e.fit", tmp_path / "f.fit", "tie"),
        make_record(tmp_path / "g.fit", tmp_path / "h.fit", "reject"),
        make_record(tmp_path / "i.fit", tmp_path / "j.fit", "left"),
    ]
    scores = {
        tmp_path / "a.fit": 2.0,
        tmp_path / "b.fit": 1.0,
        tmp_path / "c.fit": 4.0,
        tmp_path / "d.fit": 5.0,
        tmp_path / "e.fit": 10.0,
        tmp_path / "f.fit": 10.05,
        tmp_path / "i.fit": 1.0,
        tmp_path / "j.fit": 3.0,
    }

    result = evaluation.evaluate_pairwise_scores(records, scores, epsilon=0.1)

    assert result.non_tie_total == 3
    assert result.non_tie_correct == 2
    assert result.non_tie_accuracy == 2 / 3
    assert result.rejected == 1
    assert result.skipped == 0
    assert len(result.violations) == 1
    assert result.violations[0].record.left_path == tmp_path / "i.fit"


def test_collect_scores_ignores_tie_only_paths(monkeypatch, tmp_path):
    records = [
        make_record(tmp_path / "a.fit", tmp_path / "b.fit", "left"),
        make_record(tmp_path / "c.fit", tmp_path / "d.fit", "tie"),
    ]
    scored_paths = []

    def fake_measure_metric(_metric_name, sub_path, _siril_path, _timeout):
        scored_paths.append(sub_path)
        return {"score": 1.0}

    monkeypatch.setattr(commands.metrics, "measure_metric", fake_measure_metric)

    scores = commands.collect_scores(records, "stellar_quality", "siril", 1.0)

    assert set(scored_paths) == {tmp_path / "a.fit", tmp_path / "b.fit"}
    assert set(scores) == {tmp_path / "a.fit", tmp_path / "b.fit"}


def test_format_metric_measurement_includes_star_flux_components():
    text = commands.format_metric_measurement(
        {
            "score": 0.25,
            "median_mean_star_flux": 1.5,
            "background": 10.0,
            "bgnoise": 6.0,
            "star_count": 12,
        }
    )

    assert "score=0.25" in text
    assert "median mean star flux=1.5" in text
    assert "background=10" in text
    assert "bgnoise=6" in text
    assert "star count=12" in text


def test_rank_metric_results_orders_by_accuracy(tmp_path):
    record = make_record(tmp_path / "a.fit", tmp_path / "b.fit", "left")
    weak = evaluation.MetricComparisonResult(
        name="weak",
        evaluation=evaluation.evaluate_pairwise_scores([record], {tmp_path / "a.fit": 0.0, tmp_path / "b.fit": 1.0}),
        scores={},
        measurements={},
    )
    strong = evaluation.MetricComparisonResult(
        name="strong",
        evaluation=evaluation.evaluate_pairwise_scores([record], {tmp_path / "a.fit": 1.0, tmp_path / "b.fit": 0.0}),
        scores={},
        measurements={},
    )

    ranked = evaluation.rank_metric_results([weak, strong])

    assert [result.name for result in ranked] == ["strong", "weak"]


def test_score_rows_can_be_written_as_csv(tmp_path):
    output_path = tmp_path / "scores.csv"

    commands.write_score_rows(
        {tmp_path / "a.fit": {"score": 1.25, "star_count": 3, "bgnoise": 2.0}},
        "stellar_quality",
        output_path,
        "csv",
    )

    text = output_path.read_text(encoding="utf-8")
    assert "sub_path,metric,score,star_count" in text
    assert "stellar_quality,1.25,3" in text
