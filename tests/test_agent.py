import io
import threading

from flashforge_obico.agent import EXIT_SHARED_TOKEN, Agent
from flashforge_obico.camera.mjpeg_source import MjpegSource
from flashforge_obico.config import load_config
from flashforge_obico.flashforge.client import FlashforgeError

ENV = {"FF_HOST": "10.0.0.10", "FF_SERIAL": "SN", "FF_CHECK_CODE": "CC", "OBICO_AUTH_TOKEN": "TK",
       "PUBLIC_HOST": "10.0.0.2"}


class FakeObico:
    def __init__(self):
        self.sent, self.pics, self.events = [], [], []
        self.shared_token_detected = threading.Event()
        self.connected = threading.Event()
        self.connected.set()

    def send(self, message):
        self.sent.append(message)

    def post_pic(self, jpeg, **kwargs):
        self.pics.append((jpeg, kwargs))
        return True

    def post_printer_event(self, **kwargs):
        self.events.append(kwargs)
        return True


class ScriptedPrinter:
    """Returns detail dicts in order; an Exception entry raises. The last entry repeats forever."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.commands = []

    def detail(self):
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        return dict(reply)

    def pause(self):
        self.commands.append("pause")

    def resume(self):
        self.commands.append("resume")

    def cancel(self):
        self.commands.append("cancel")


class FakeClock:
    def __init__(self, start=1000.0):
        self.t = start

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


def make(detail_replies, cameras=None, clock=None):
    clock = clock or FakeClock()
    agent = Agent(load_config(ENV), ScriptedPrinter(detail_replies), FakeObico(), cameras or [],
                  clock=clock, monotonic=clock, sleep=clock.sleep)
    return agent, clock


def test_first_poll_sends_settings_then_changes_or_heartbeat_only(detail):
    printing = {**detail, "status": "printing", "printFileName": "a.gcode", "printProgress": 0.1}
    agent, clock = make([detail, detail, printing])
    agent.poll_once()
    first = agent.obico.sent[0]
    assert first["status"]["state"]["text"] == "Operational" and first["current_print_ts"] == -1
    assert first["settings"]["webcams"][0]["stream_url"] == "http://10.0.0.2:8081/cameras/0/stream"
    assert first["settings"]["webcams"][0]["name"] == "Printer"
    agent.poll_once()  # unchanged, inside the heartbeat window: nothing sent
    assert len(agent.obico.sent) == 1
    agent.poll_once()  # job started: status carries the event
    assert agent.obico.sent[1]["event"] == {"event_type": "PrintStarted"}
    assert agent.obico.sent[1]["status"]["state"]["text"] == "Printing"
    assert "settings" not in agent.obico.sent[1]
    clock.t += 31
    agent.poll_once()
    assert len(agent.obico.sent) == 3 and "event" not in agent.obico.sent[2]
    assert agent.poll_interval() == 2.0


def test_two_events_in_one_poll_are_sent_separately(detail):
    a = {**detail, "status": "printing", "printFileName": "a.gcode", "printProgress": 0.5}
    b = {**detail, "status": "printing", "printFileName": "b.gcode"}
    agent, _ = make([a, b])
    agent.poll_once()
    agent.poll_once()
    assert [m["event"]["event_type"] for m in agent.obico.sent[1:]] == ["PrintFailed", "PrintStarted"]


def test_three_failures_mean_offline_and_never_a_command(detail):
    agent, _ = make([detail, FlashforgeError("x"), FlashforgeError("x"), FlashforgeError("x")])
    agent.poll_once()
    agent.poll_once()
    agent.poll_once()
    assert agent.obico.sent[-1]["status"]["state"]["text"] == "Operational"  # two failures: still the last state
    agent.poll_once()
    assert agent.obico.sent[-1]["status"] == {} and agent.printer.commands == []
    assert agent.poll_interval() == 5.0


def test_pause_command_is_forwarded_and_read_back(detail):
    printing = {**detail, "status": "printing", "printFileName": "a.gcode", "printDuration": 30, "printLayer": 3}
    paused = {**printing, "status": "paused"}
    agent, _ = make([printing, printing, paused])
    agent.poll_once()
    agent.execute_command("pause")
    assert agent.printer.commands == ["pause"] and agent.obico.events == []


def test_printer_refusal_raises_a_printer_event(detail):
    printing = {**detail, "status": "printing", "printFileName": "a.gcode", "printDuration": 30, "printLayer": 3}
    agent, _ = make([printing])
    agent.poll_once()

    def refuse():
        raise FlashforgeError("control: printer returned 5: refused")

    agent.printer.cancel = refuse
    agent.execute_command("cancel")
    assert agent.obico.events[0]["title"] == "Cancel failed"


def test_commands_are_idempotent_and_need_a_job(detail):
    paused = {**detail, "status": "pause", "printFileName": "a.gcode", "printDuration": 30, "printLayer": 3}
    agent, _ = make([paused])
    agent.poll_once()
    agent.execute_command("pause")  # already paused
    assert agent.printer.commands == []
    idle, _ = make([detail])
    idle.poll_once()
    idle.execute_command("cancel")
    assert idle.printer.commands == []


def test_server_messages_dispatch_commands_and_viewing(detail):
    printing = {**detail, "status": "printing", "printFileName": "a.gcode", "printDuration": 30, "printLayer": 3}
    agent, _ = make([printing, {**printing, "status": "pause"}])
    agent.poll_once()
    agent.handle_server_message({"remote_status": {"viewing": True}})
    assert agent.viewing is True
    agent.handle_server_message({"commands": [{"cmd": "pause"}, {"cmd": "selfdestruct"}, "junk"]})
    assert agent.wait_for_commands(timeout=2.0)
    assert agent.printer.commands == ["pause"]


def test_primary_frame_posting_respects_freshness(detail):
    clock = FakeClock()
    cam = MjpegSource("http://x", name="Printer", opener=lambda url: io.BytesIO(b""), clock=clock)
    agent, _ = make([detail], cameras=[cam], clock=clock)
    assert agent.post_primary_frame() is False  # no frame yet
    cam._store(b"jpg")
    assert agent.post_primary_frame() is True
    assert agent.obico.pics[0] == (b"jpg", {"camera_name": "Printer", "is_primary": True, "viewing_boost": False})
    clock.t += 16
    assert agent.post_primary_frame() is False  # stale


def test_hard_fault_posts_one_event_per_code(detail):
    faulty = {**detail, "status": "error", "errorCode": "E7"}
    agent, _ = make([faulty])
    agent.poll_once()
    agent.poll_once()
    assert [e["title"] for e in agent.obico.events] == ["Printer error E7"]


def test_run_exits_when_the_token_is_shared(detail):
    agent, _ = make([detail])
    agent.obico.shared_token_detected.set()
    assert agent.run(threading.Event()) == EXIT_SHARED_TOKEN


# ── Job control: verified behaviour of firmware 1.9.9 on 2026-09-15 ────────────────────────────
# The printer acknowledges a pause with code 0 during warm-up and then ignores it; only once
# extrusion has begun does a pause take. These tests pin the agent's answer to that.

def _printing(detail, **over):
    base = {**detail, "status": "printing", "printFileName": "a.gcode", "printDuration": 30, "printLayer": 3}
    base.update(over)
    return base


def test_pause_that_takes_on_first_readback(detail):
    printing = _printing(detail)
    paused = {**printing, "status": "pause"}                  # the firmware's real string
    agent, _ = make([printing, printing, paused])
    agent.poll_once()
    agent.execute_command("pause")
    assert agent.printer.commands == ["pause"] and agent.obico.events == []
    assert agent.obico.sent[-1]["status"]["state"]["text"] == "Paused"   # status sent as soon as it took


def test_pause_is_resent_when_the_first_attempt_does_not_take(detail):
    printing = _printing(detail)
    paused = {**printing, "status": "pause"}
    # one attempt = 1 send + READBACK_ATTEMPTS polls; the second attempt sees the pause take
    replies = [printing] + [printing] * 5 + [paused]
    agent, _ = make(replies)
    agent.poll_once()
    agent.execute_command("pause")
    assert agent.printer.commands == ["pause", "pause"] and agent.obico.events == []


def test_pause_that_never_takes_while_really_printing_raises_one_event(detail):
    printing = _printing(detail)
    agent, _ = make([printing])
    agent.poll_once()
    agent.execute_command("pause")
    assert agent.printer.commands == ["pause"] * 3                      # MAX_COMMAND_ATTEMPTS
    assert [e["title"] for e in agent.obico.events] == ["Pause did not take"]
    assert agent.obico.sent[-1]["status"]["state"]["text"] == "Printing"  # Obico told the truth right away


def test_pause_during_warm_up_stays_pending_and_lands_when_printing_starts(detail):
    warming = _printing(detail, printDuration=0, printLayer=0)
    started = _printing(detail, printDuration=5, printLayer=1)
    paused = {**started, "status": "pause"}
    # attempts during warm-up: 3 sends x 5 polls all warming; then polls see printing start; the
    # pending pause fires once and the read-back sees "pause"
    replies = [warming] + [warming] * 16 + [started, paused]
    agent, _ = make(replies)
    agent.poll_once()
    agent.execute_command("pause")
    assert agent.printer.commands == ["pause"] * 3
    assert agent.pending_command == "pause"
    assert [e["title"] for e in agent.obico.events] == ["Pause deferred until printing starts"]
    agent.poll_once()                       # still warming: nothing new sent
    assert agent.printer.commands == ["pause"] * 3
    agent.poll_once()                       # extrusion has started: the pending pause fires
    assert agent.printer.commands == ["pause"] * 4
    assert agent.pending_command is None
    assert agent.tracker.paused is True


def test_resume_or_cancel_clears_a_pending_pause(detail):
    warming = _printing(detail, printDuration=0, printLayer=0)
    agent, _ = make([warming])
    agent.poll_once()
    agent.execute_command("pause")
    assert agent.pending_command == "pause"
    agent.execute_command("resume")         # the user changed their mind: nothing to resume, intent dropped
    assert agent.pending_command is None
    assert agent.printer.commands.count("resume") == 0


def test_job_end_clears_a_pending_pause(detail):
    warming = _printing(detail, printDuration=0, printLayer=0)
    agent, _ = make([warming] * 16 + [detail])   # 1 poll + 3 attempts x 5 read-backs, then the job is gone
    agent.poll_once()
    agent.execute_command("pause")
    assert agent.pending_command == "pause"
    agent.poll_once()                       # job gone
    assert agent.pending_command is None
