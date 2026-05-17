from sky_scripter.project import ProgressStore, ProjectPlan, sanitize_name


def _project_data():
    return {
        "latitude": 30.0,
        "longitude": -97.0,
        "targets": [{
            "target": "M31",
            "filters": {
                "L": {"exposure": 300, "target_frames": 3},
                "R": {"exposure": 300, "target_frames": 1},
            },
            "min_altitude": 30,
            "dither_every": 2,
        }],
    }


def test_project_generates_remaining_night_plan(tmp_path):
    project_path = tmp_path / "project.json"
    project_path.write_text(__import__("json").dumps(_project_data()))
    project = ProjectPlan.load(project_path)
    store = ProgressStore(tmp_path)
    path = store.next_filename("M31", "L")
    open(path, "wb").write(b"fits")

    plan, session_map = project.to_night_plan(store)

    assert len(plan.sessions) == 1
    assert session_map == {0: "M31"}
    assert plan.sessions[0].filters == [("L", 300.0, 2), ("R", 300.0, 1)]


def test_project_progress_counts_existing_files(tmp_path):
    project_path = tmp_path / "project.json"
    project_path.write_text(__import__("json").dumps(_project_data()))
    project = ProjectPlan.load(project_path)
    store = ProgressStore(tmp_path)
    open(store.next_filename("M31", "L"), "wb").write(b"fits")

    summary = project.progress_summary(store)

    l_filter = summary[0]["filters"][0]
    assert l_filter["accepted"] == 1
    assert l_filter["remaining"] == 2


def test_project_omits_complete_targets(tmp_path):
    project_path = tmp_path / "project.json"
    project_path.write_text(__import__("json").dumps(_project_data()))
    project = ProjectPlan.load(project_path)
    store = ProgressStore(tmp_path)
    for i in range(3):
        open(store.next_filename("M31", "L"), "wb").write(f"fits{i}".encode())
    open(store.next_filename("M31", "R"), "wb").write(b"fits")

    plan, _ = project.to_night_plan(store)

    assert plan.sessions == []


def test_progress_store_uses_sanitized_project_filter_paths(tmp_path):
    store = ProgressStore(tmp_path)

    path = store.next_filename("M 31 / Andromeda", "Ha/OIII")
    open(path, "wb").write(b"fits")

    assert path == str(
        tmp_path / "M_31_Andromeda" / "Ha_OIII" /
        "M_31_Andromeda-Ha_OIII-00001.fits"
    )
    assert store.next_filename("M 31 / Andromeda", "Ha/OIII").endswith(
        "M_31_Andromeda-Ha_OIII-00002.fits")
    assert sanitize_name("M 31 / Andromeda") == "M_31_Andromeda"


def test_night_plan_uses_default_schedule_offsets(tmp_path):
    project_path = tmp_path / "project.json"
    project_path.write_text(__import__("json").dumps(_project_data()))
    project = ProjectPlan.load(project_path)
    store = ProgressStore(tmp_path)

    plan, _ = project.to_night_plan(
        store, default_start_offset=-30, default_end_offset=30)

    assert plan.sessions[0].start_offset == -30
    assert plan.sessions[0].end_offset == 30


def test_project_offsets_override_default_schedule_offsets(tmp_path):
    data = _project_data()
    data["targets"][0]["start_offset"] = -10
    data["targets"][0]["end_offset"] = 20
    project_path = tmp_path / "project.json"
    project_path.write_text(__import__("json").dumps(data))
    project = ProjectPlan.load(project_path)
    store = ProgressStore(tmp_path)

    plan, _ = project.to_night_plan(
        store, default_start_offset=-30, default_end_offset=30)

    assert plan.sessions[0].start_offset == -10
    assert plan.sessions[0].end_offset == 20
