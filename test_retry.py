#!/usr/bin/env python3
"""Unit tests for the outgoing-message retry mechanism in ui.PagerUI.

Run on the pager (no LoRa or E-Ink needed -- both are mocked):
    sudo python3 test_retry.py

Covers:
  1. Initial send sets status=SENDING, retries=0.
  2. No retry fires before ACK_TIMEOUT elapses.
  3. After ACK_TIMEOUT, retry fires with the SAME msg_id, retries increments.
  4. Exactly MAX_RETRIES retransmits occur, then status flips to FAIL.
  5. Ack arriving mid-retry halts the retry sequence and marks OK.
  6. Duplicate incoming (same msg_id) is deduped.
  7. Duplicate incoming still gets acked by main.py's convention (tested
     at the lora level via the mock -- ui just dedupes).

Exits 0 on success, 1 on any assertion failure.
"""
import sys
import types
import time

# Mock display_eink BEFORE importing ui. The real module runs a hardware
# reset at import time, which we obviously can't do in a unit test.
class _FakeEink:
    def __init__(self): pass
    def draw_chat(self, *a, **k): pass
    def draw_profile(self, *a, **k): pass
    def draw_name_edit(self, *a, **k): pass
    def draw_channel_edit(self, *a, **k): pass
    def draw_sleep(self, *a, **k): pass
    def cleanup(self): pass
    def sleep(self): pass

_fake = types.ModuleType("display_eink")
_fake.EInkDisplay = _FakeEink
sys.modules["display_eink"] = _fake

sys.path.insert(0, "/home/pi/kidpager")

# Re-route history to a throwaway temp path so tests don't touch real history.
import os, tempfile
_tmp = tempfile.mkdtemp(prefix="kidpager-test-")
os.environ["HOME"] = _tmp

from ui import (PagerUI, Message,
                STATUS_SENDING, STATUS_OK, STATUS_FAIL, STATUS_LOCAL,
                ACK_TIMEOUT, MAX_RETRIES)


class MockLoRa:
    """Minimal LoRa stub that records every send call."""
    def __init__(self):
        self.sent = []   # list of (sender, text, msg_id)
    def send(self, sender, text, msg_id=None):
        effective_id = msg_id or f"auto{len(self.sent):04d}"
        self.sent.append((sender, text, effective_id))
        return effective_id
    def send_ack(self, msg_id):
        pass


class FakeConfig:
    def __init__(self):
        self.name = "TestA"
        self.channel = 1
        self.silent = False
    def save(self): pass
    def load(self): pass


# ---------------------------------------------------------------------------
# Test harness
# ---------------------------------------------------------------------------
_passed = 0
_failed = 0


def check(cond, label):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  OK   {label}")
    else:
        _failed += 1
        print(f"  FAIL {label}")


def new_ui():
    """Fresh UI with a mock LoRa, no history loaded."""
    ui = PagerUI(FakeConfig(), lora=MockLoRa())
    ui.messages = []   # in case fixture file polluted
    return ui


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_initial_send_state():
    print("\n[1] initial send state")
    ui = new_ui()
    ui.add_message("TestA", "hello", outgoing=True, msg_id="m001")
    m = ui.messages[-1]
    check(m.status == STATUS_SENDING, "status == SENDING after send")
    check(m.retries == 0,             "retries == 0 initially")
    check(m.msg_id == "m001",         "msg_id preserved")


def test_no_retry_before_timeout():
    print("\n[2] no retry before ACK_TIMEOUT")
    ui = new_ui()
    ui.add_message("TestA", "hello", outgoing=True, msg_id="m002")
    m = ui.messages[-1]
    m.last_sent_ts = time.time() - (ACK_TIMEOUT - 1)
    changed = ui.check_timeouts()
    check(not changed,                "check_timeouts returns False under timeout")
    check(m.retries == 0,             "retries unchanged")
    check(m.status == STATUS_SENDING, "status unchanged")
    check(len(ui.lora.sent) == 0,     "lora.send NOT called")


def test_retry_fires_after_timeout():
    print("\n[3] retry fires after ACK_TIMEOUT")
    ui = new_ui()
    ui.add_message("TestA", "hello world", outgoing=True, msg_id="m003")
    m = ui.messages[-1]
    # Age the message past the threshold.
    m.last_sent_ts = time.time() - (ACK_TIMEOUT + 1)
    changed = ui.check_timeouts()
    check(changed,                     "check_timeouts returns True")
    check(m.retries == 1,              "retries == 1 after first retransmit")
    check(m.status == STATUS_SENDING,  "status still SENDING (not yet FAIL)")
    check(len(ui.lora.sent) == 1,      "lora.send called exactly once")
    sender, text, msg_id = ui.lora.sent[0]
    check(msg_id == "m003",            "retry used SAME msg_id (for receiver dedup)")
    check(text == "hello world",       "retry used same text")


def test_retries_exhaust_to_fail():
    print("\n[4] MAX_RETRIES retransmits then FAIL")
    ui = new_ui()
    ui.add_message("TestA", "ping", outgoing=True, msg_id="m004")
    m = ui.messages[-1]

    # Fire check_timeouts once per retry window. Each time we age the message
    # past ACK_TIMEOUT, then call check. After MAX_RETRIES retries, status
    # should flip to FAIL on the *next* call.
    for i in range(MAX_RETRIES):
        m.last_sent_ts = time.time() - (ACK_TIMEOUT + 1)
        ui.check_timeouts()
        check(m.retries == i + 1,            f"retry #{i+1} recorded (retries={m.retries})")
        check(m.status == STATUS_SENDING,    f"still SENDING after retry #{i+1}")

    # One more timeout with no more retries available -> FAIL.
    m.last_sent_ts = time.time() - (ACK_TIMEOUT + 1)
    ui.check_timeouts()
    check(m.status == STATUS_FAIL,          "status == FAIL after exhausting retries")
    check(len(ui.lora.sent) == MAX_RETRIES, f"lora.send called exactly {MAX_RETRIES} times total")


def test_ack_halts_retry():
    print("\n[5] ack halts retry sequence")
    ui = new_ui()
    ui.add_message("TestA", "urgent", outgoing=True, msg_id="m005")
    m = ui.messages[-1]

    # One retry fires.
    m.last_sent_ts = time.time() - (ACK_TIMEOUT + 1)
    ui.check_timeouts()
    check(m.retries == 1, "first retry fired")

    # Ack arrives.
    delivered = ui.mark_delivered("m005")
    check(delivered,                      "mark_delivered returned True")
    check(m.status == STATUS_OK,          "status == OK after ack")

    # Further timeout scans must not retransmit an OK message.
    sent_count_before = len(ui.lora.sent)
    m.last_sent_ts = time.time() - (ACK_TIMEOUT + 10)
    ui.check_timeouts()
    check(len(ui.lora.sent) == sent_count_before, "no retry after ack")
    check(m.status == STATUS_OK,          "status still OK")


def test_incoming_dedup():
    print("\n[6] incoming dedup (same msg_id)")
    ui = new_ui()
    added_1 = ui.add_message("Bob", "hi", outgoing=False, msg_id="x001")
    check(added_1,                  "first incoming returns True (added)")
    check(len(ui.messages) == 1,    "one message in history")

    # Sender's ack was lost, they resend. Our dedup must catch it.
    added_2 = ui.add_message("Bob", "hi", outgoing=False, msg_id="x001")
    check(not added_2,              "duplicate incoming returns False (skipped)")
    check(len(ui.messages) == 1,    "still one message in history")

    # Different msg_id from same sender -> fresh message.
    added_3 = ui.add_message("Bob", "hi", outgoing=False, msg_id="x002")
    check(added_3,                  "new msg_id accepted even with same text")
    check(len(ui.messages) == 2,    "two messages in history")


def test_no_lora_graceful():
    print("\n[7] retry with lora=None does not crash")
    ui = PagerUI(FakeConfig(), lora=None)
    ui.messages = []
    ui.add_message("TestA", "orphan", outgoing=True, msg_id="m007")
    m = ui.messages[-1]
    m.last_sent_ts = time.time() - (ACK_TIMEOUT + 1)
    # With lora=None we can't retransmit -> status should go straight to FAIL.
    changed = ui.check_timeouts()
    check(changed,                     "check_timeouts returned True")
    check(m.status == STATUS_FAIL,     "status == FAIL when lora is None")


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
def main():
    print("=== KidPager retry unit tests ===")
    print(f"  ACK_TIMEOUT = {ACK_TIMEOUT}s, MAX_RETRIES = {MAX_RETRIES}")

    test_initial_send_state()
    test_no_retry_before_timeout()
    test_retry_fires_after_timeout()
    test_retries_exhaust_to_fail()
    test_ack_halts_retry()
    test_incoming_dedup()
    test_no_lora_graceful()

    print(f"\n=== {_passed} passed, {_failed} failed ===")
    sys.exit(0 if _failed == 0 else 1)


if __name__ == "__main__":
    main()
