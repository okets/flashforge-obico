from flashforge_obico.flashforge.snapshot import parse_snapshot
from flashforge_obico.job_tracker import Event, JobTracker


def snap(detail, **over):
    merged = dict(detail)
    merged.update(over)
    return parse_snapshot(merged)


def test_start_pause_resume_done(detail):
    t = JobTracker()
    assert t.observe(snap(detail), 100.0) == [] and t.current_print_ts == -1
    assert t.observe(snap(detail, status="heating", printFileName="a.gcode"), 101.0) == [Event.STARTED]
    assert t.current_print_ts == 101 and t.active_file == "a.gcode"
    assert t.observe(snap(detail, status="printing", printFileName="a.gcode", printProgress=0.3), 102.0) == []
    assert t.observe(snap(detail, status="paused", printFileName="a.gcode", printProgress=0.3), 103.0) == [Event.PAUSED]
    assert t.observe(snap(detail, status="paused", printFileName="a.gcode", printProgress=0.3), 104.0) == []
    assert t.observe(snap(detail, status="printing", printFileName="a.gcode", printProgress=0.4), 105.0) == [Event.RESUMED]
    assert t.observe(snap(detail, status="completed", printFileName="a.gcode", printProgress=1.0), 106.0) == [Event.DONE]
    assert t.current_print_ts == -1 and t.active_file is None
    assert t.observe(snap(detail, status="ready"), 107.0) == []


def test_cancelled_and_error(detail):
    t = JobTracker()
    t.observe(snap(detail, status="printing", printFileName="a.gcode"), 1.0)
    assert t.observe(snap(detail, status="cancel", printFileName="a.gcode"), 2.0) == [Event.CANCELLED]
    t.observe(snap(detail, status="printing", printFileName="b.gcode"), 3.0)
    assert t.observe(snap(detail, status="error", printFileName="b.gcode", errorCode="E123"), 4.0) == [Event.FAILED]


def test_file_vanishing_mid_print_is_a_failure_but_at_the_end_is_done(detail):
    t = JobTracker()
    t.observe(snap(detail, status="printing", printFileName="a.gcode", printProgress=0.5), 1.0)
    assert t.observe(snap(detail, status="ready"), 2.0) == [Event.FAILED]
    t.observe(snap(detail, status="printing", printFileName="a.gcode", printProgress=0.999), 3.0)
    assert t.observe(snap(detail, status="ready"), 4.0) == [Event.DONE]


def test_file_swap_ends_old_and_starts_new(detail):
    t = JobTracker()
    t.observe(snap(detail, status="printing", printFileName="a.gcode", printProgress=0.5), 1.0)
    assert t.observe(snap(detail, status="printing", printFileName="b.gcode"), 2.0) == [Event.FAILED, Event.STARTED]
    assert t.current_print_ts == 2 and t.active_file == "b.gcode"


def test_unknown_and_offline_change_nothing(detail):
    t = JobTracker()
    t.observe(snap(detail, status="printing", printFileName="a.gcode"), 1.0)
    assert t.observe(snap(detail, status="mystery", printFileName="a.gcode"), 2.0) == []
    assert t.observe(None, 3.0) == []
    assert t.current_print_ts == 1 and t.active_file == "a.gcode"


def test_same_file_printed_twice(detail):
    t = JobTracker()
    t.observe(snap(detail, status="printing", printFileName="a.gcode"), 1.0)
    t.observe(snap(detail, status="completed", printFileName="a.gcode", printProgress=1.0), 2.0)
    assert t.observe(snap(detail, status="printing", printFileName="a.gcode"), 3.0) == [Event.STARTED]


def test_job_that_starts_paused_reports_resume_later(detail):
    t = JobTracker()
    assert t.observe(snap(detail, status="paused", printFileName="a.gcode"), 1.0) == [Event.STARTED]
    assert t.paused is True
    assert t.observe(snap(detail, status="printing", printFileName="a.gcode"), 2.0) == [Event.RESUMED]
