import math


FormulaCandidate = tuple[str, str, float, float, float, float]


def formula_sweep_candidates() -> list[FormulaCandidate]:
    candidates = []
    star_terms = [
        ("star_count^0", 0.0),
        ("star_count^0.25", 0.25),
        ("star_count^0.5", 0.5),
        ("star_count^0.75", 0.75),
        ("log1p(star_count)", math.nan),
    ]
    flux_exponents = [0.75, 1.0, 1.25]
    bgnoise_exponents = [0.5, 1.0, 1.5]
    background_exponents = [0.25, 0.5, 1.0]
    for star_term_name, star_exponent in star_terms:
        for flux_exponent in flux_exponents:
            for bgnoise_exponent in bgnoise_exponents:
                for background_exponent in background_exponents:
                    name = (
                        f"sweep:{star_term_name}*signed_flux^{flux_exponent:g}"
                        f"/(bgnoise^{bgnoise_exponent:g}*background^{background_exponent:g})"
                    )
                    candidates.append(
                        (
                            name,
                            star_term_name,
                            star_exponent,
                            flux_exponent,
                            bgnoise_exponent,
                            background_exponent,
                        )
                    )
    return candidates


def signed_power(value: float, exponent: float) -> float:
    if not math.isfinite(value):
        return float("nan")
    if value == 0.0:
        return 0.0
    return math.copysign(abs(value) ** exponent, value)


def formula_sweep_score(
    feature_values: dict[str, float | int],
    *,
    star_exponent: float,
    flux_exponent: float = 1.0,
    bgnoise_exponent: float,
    background_exponent: float,
) -> float:
    star_count = float(feature_values["star_count"])
    flux = float(feature_values["median_mean_star_flux"])
    background = float(feature_values["background"])
    bgnoise = float(feature_values["bgnoise"])
    values = (star_count, flux, background, bgnoise)
    if not all(math.isfinite(value) for value in values):
        return float("nan")
    if bgnoise <= 0.0:
        return float("nan")
    if math.isnan(star_exponent):
        star_factor = math.log1p(max(star_count, 0.0))
    else:
        star_factor = max(star_count, 0.0) ** star_exponent
    denominator = (bgnoise ** bgnoise_exponent) * (max(background, 1.0e-12) ** background_exponent)
    if denominator <= 0.0 or not math.isfinite(denominator):
        return float("nan")
    return float(star_factor * signed_power(flux, flux_exponent) / denominator)
