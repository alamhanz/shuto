from shuto.core.paths import folder_name
from shuto.core.progress import ScaledProgress
from shuto.core.text import yes_no


def test_scaled_progress_maps_onto_part_of_a_bar():
    seen = []
    first = ScaledProgress(lambda d, t, m: seen.append((d, t, m)), 0, 100, 1000)
    second = ScaledProgress(lambda d, t, m: seen.append((d, t, m)), 100, 900, 1000)
    first(5, 10, "a")
    second(1, 3, "b")
    second(0, 0)
    assert seen == [(50, 1000, "a"), (400, 1000, "b"), (100, 1000, "")]


def test_folder_name_lives_in_core_and_is_still_in_report():
    from shuto.deliver import report

    assert report.folder_name is folder_name
    assert folder_name("trip (2)") == "trip (2)"


def test_yes_no():
    assert (yes_no(1), yes_no(None)) == ("yes", "no")
