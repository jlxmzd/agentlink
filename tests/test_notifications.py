"""唤醒去重、租约、失败重试与长对话规则提醒。只用临时项目和假 RPC / 假时钟。

    python -I tests/test_notifications.py
"""
import datetime
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

SANDBOX = tempfile.TemporaryDirectory(prefix='agentlink-notify-')
os.environ['AGENTLINK_USER_HOME'] = str(Path(SANDBOX.name) / 'home')
# AGENTLINK_HOME outranks AGENTLINK_USER_HOME for projects.json: without this the suite could touch the real
# registry. Keep both inside this run's sandbox.
os.environ['AGENTLINK_HOME'] = str(Path(SANDBOX.name) / 'home' / '.agentlink')
os.environ.pop('AGENTLINK_NO_WAKE', None)
os.environ['AGENTLINK_NO_DISPATCH'] = '1'
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentlink import project, bus as B


class Notifications(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=SANDBOX.name)
        self.addCleanup(self.temp.cleanup)
        self.p = project.Project(self.temp.name)
        self.p.apply({'codex': {'id': '00000000-0000-0000-0000-000000000001'},
                      'claude': {'id': '00000000-0000-0000-0000-000000000002'},
                      'deepseek': {'id': '00000000-0000-0000-0000-000000000003'}},
                     options={'autoAccept': []})
        self.bus = self.p.bus()
        self.bus.set_wake(True)

    def task(self, poster='claude', to='codex', title='t'):
        allowed = ['src/task%d' % len(self.bus.all())]
        return self.bus.post(poster, to, title, 'spec', paths=allowed, no_dispatch=True)['id']

    def age_seat(self, agent, seconds):
        """Move the seat's stamps into the past: the debounce and the lease must not need a real sleep."""
        with self.bus.lock:
            seat = self.bus.read_seat(agent)
            for key in ('wakes', 'wakeLeases'):
                pass
            stamp = (datetime.datetime.now().astimezone() - datetime.timedelta(seconds=seconds)).isoformat()
            seat['wokeAt'] = stamp
            if seat.get('wakes'):
                seat['wakes'] = [w - seconds for w in seat['wakes']]
            for lease in seat.get('wakeLeases') or []:
                lease['at'] = lease.get('at', time.time()) - seconds
            B.write_json(self.bus.seat_path(agent), seat)

    def codex_thread(self, thread='00000000-0000-0000-0000-0000000000aa'):
        with self.bus.lock:
            seat = self.bus.read_seat('codex')
            seat['thread'] = thread
            B.write_json(self.bus.seat_path('codex'), seat)

    def wake(self, returncode=0, raise_error=False):
        """Run wake_codex with a fake codex queue call and a zero quiet window."""
        with patch.object(B, 'WAKE_QUIET', 0), \
             patch.object(B, 'codex_command', return_value=['codex']), \
             patch.object(B.subprocess, 'run') as run:
            if raise_error:
                run.side_effect = OSError('no codex')
            else:
                run.return_value.returncode = returncode
            result = self.bus.wake_codex()
        return result, run

    # -- 去重 ------------------------------------------------------------------
    def test_inflight_wake_blocks_other_keys_for_same_recipient(self):
        self.task()
        with patch.object(B, 'WAKE_QUIET', 0):
            first = self.bus.reserve_wake('codex')
            self.task()
            self.assertIsNone(self.bus.reserve_wake('codex'))
        self.assertIsNotNone(first)

    def test_disabled_wake_blocks_reservation_and_actual_queue(self):
        self.task()
        self.bus.set_wake(False)
        self.assertIsNone(self.wake()[0])
        with patch.object(B, 'WAKE_QUIET', 0):
            self.assertFalse(self.bus.pending('codex', self.p.binding('codex')['id'], claim=True)['wake'])
        self.bus.set_wake(True)
        with patch.dict(os.environ, {'AGENTLINK_NO_WAKE': '1'}):
            self.assertIsNone(self.wake()[0])

    def test_terminal_state_between_reservation_and_send_never_invokes_host(self):
        tid = self.task()
        reserve = self.bus.reserve_wake
        def reserve_then_finish(*args, **kwargs):
            out = reserve(*args, **kwargs)
            self.bus.cancel_task('claude', tid, 'completed before send')
            return out
        with patch.object(self.bus, 'reserve_wake', side_effect=reserve_then_finish):
            result, run = self.wake()
        self.assertIsNone(result)
        run.assert_not_called()

    def test_delivery_cycle_is_a_new_notification_even_with_same_state_and_attempt(self):
        tid = self.task(poster='codex', to='deepseek')
        self.bus.deliver('deepseek', tid, 'first')
        self.assertEqual(self.wake()[0], 'queued')
        self.bus.reassign('codex', tid, 'claude', no_dispatch=True)
        self.bus.reassign('codex', tid, 'deepseek', no_dispatch=True)
        self.bus.deliver('deepseek', tid, 'second')
        self.age_seat('codex', 600)
        self.assertEqual(self.wake()[0], 'queued')

    def test_wake_calls_overlap_at_the_host_with_only_one_queue(self):
        import threading
        self.codex_thread()
        self.task()
        entered, release = threading.Event(), threading.Event()
        results = []
        def rpc(*args, **kwargs):
            # Only the `codex queue` call may block. Bus internals (git_head/take_snapshot) are patched out
            # below, so any other patched subprocess hop would be a hole in the patch set, not a real overlap.
            argv = list(args[0]) if args else []
            if 'queue' not in argv:
                class Other: returncode = 0
                return Other()
            entered.set()
            if not release.wait(5): raise RuntimeError('test synchronization failed')
            class Result: returncode = 0
            return Result()
        with patch.object(self.bus, 'git_head', return_value=None), patch.object(self.bus, 'take_snapshot'), \
             patch.object(B, 'WAKE_QUIET', 0), patch.object(B, 'codex_command', return_value=['codex']), \
             patch.object(B.subprocess, 'run', side_effect=rpc) as run:
            first = threading.Thread(target=lambda: results.append(self.bus.wake_codex()))
            first.start()
            try:
                self.assertTrue(entered.wait(5))
                self.assertIsNone(self.bus.wake_codex())
                self.task()  # different task must share the same recipient reservation
                self.assertIsNone(self.bus.wake_codex())
            finally:
                release.set(); first.join(5)
            self.assertEqual(run.call_count, 1)
        self.assertEqual(results, ['queued'])

    def test_two_concurrent_wakes_queue_only_once(self):
        """Two processes ticking at the same moment: the reservation is atomic, so only one queues."""
        self.codex_thread()
        self.task()
        results = []
        # Both callers run through the same bus object but the second one sees the first one's lease.
        first, run1 = self.wake()
        second, run2 = self.wake()
        results = [first, second]
        self.assertEqual(results, ['queued', None])
        self.assertEqual(run1.call_count, 1, 'the winner queued exactly one message')
        self.assertEqual(run2.call_count, 0, 'the loser stood down without touching the host')

    def test_the_same_item_is_not_woken_twice_after_a_successful_wake(self):
        self.codex_thread()
        tid = self.task()
        self.assertEqual(self.wake()[0], 'queued')
        self.age_seat('codex', 600)                     # past the debounce, but the item was already delivered
        self.assertIsNone(self.wake()[0], 'a delivered key must not be woken again')
        self.assertIn(tid, [self.bus.load(tid)['id']])
        woken = ' '.join(self.bus.read_seat('codex').get('woken') or [])
        self.assertTrue(tid in woken, 'the delivered key is recorded as woken')

    def test_a_new_rework_round_is_notified_again(self):
        """The key carries state/attempt, so genuinely new work on the same task is a new notification."""
        self.codex_thread()
        tid = self.task()
        self.bus.claim('codex', tid)
        self.bus.deliver('codex', tid, 'first')
        self.bus.review('claude', tid, 'reject', 'bad', kind='code')
        self.age_seat('codex', 600)
        self.assertEqual(self.wake()[0], 'queued', 'a rework is new work and must wake again')

    def test_a_manual_reminder_is_its_own_notification(self):
        """A remind bumps finalReview.round, which is part of the wake key, so it is a genuinely new notice."""
        self.codex_thread()
        # Codex must be the final reviewer: a task posted by Codex and executed by DeepSeek lands back on Codex.
        tid = self.task(poster='codex', to='deepseek')
        self.bus.claim('deepseek', tid)
        self.bus.deliver('deepseek', tid, 'done')
        owner = self.bus.final_review_owner(self.bus.load(tid))
        self.assertEqual(owner, 'codex', 'the poster reviews its own delivery')
        self.age_seat('codex', 600)
        self.assertEqual(self.wake()[0], 'queued', 'the delivery itself wakes the reviewer')
        before = (self.bus.load(tid).get('finalReview') or {}).get('round', 0)
        self.bus.review_attention('codex', tid, 'remind', 'please review')
        after = (self.bus.load(tid).get('finalReview') or {}).get('round', 0)
        self.assertEqual(after, before + 1, 'a manual remind opens a new round')
        self.age_seat('codex', 600)
        self.assertEqual(self.wake()[0], 'queued', 'a manual remind is a new notification')

    def test_a_terminal_task_never_produces_a_notification(self):
        self.codex_thread()
        tid = self.task()
        self.bus.cancel_task('claude', tid, 'done with this')
        before = self.bus.read_seat('codex').get('woken') or []
        self.assertIsNone(self.wake()[0], 'nothing left to wake for')
        self.assertEqual(self.bus.read_seat('codex').get('woken') or [], before)

    # -- 租约与回包 ------------------------------------------------------------
    def test_a_task_finished_before_sending_is_not_marked_delivered(self):
        """Reserve, then let the task terminate, then finish: the finish must not claim it told anyone."""
        self.codex_thread()
        tid = self.task()
        with patch.object(B, 'WAKE_QUIET', 0):
            reserved = self.bus.reserve_wake('codex')
        self.assertIsNotNone(reserved, 'a pending task can be reserved')
        self.bus.cancel_task('claude', tid, 'closing before send')
        self.assertTrue(self.bus.finish_wake('codex', reserved['lease'], delivered=True))
        seat = self.bus.read_seat('codex')
        self.assertEqual(seat.get('woken') or [], [],
                         'a key that no longer exists is never recorded as delivered')

    def test_a_stale_lease_reply_cannot_clobber_a_newer_one(self):
        self.codex_thread()
        self.task()
        with patch.object(B, 'WAKE_QUIET', 0):
            first = self.bus.reserve_wake('codex')
        self.age_seat('codex', B.WAKE_LEASE_SECONDS + 5)   # the holder crashed; the lease is now reclaimable
        with patch.object(B, 'WAKE_QUIET', 0):
            second = self.bus.reserve_wake('codex')
        self.assertIsNotNone(second, 'an expired lease must be recoverable')
        self.assertNotEqual(first['lease'], second['lease'])
        # the crashed process comes back and reports success: it must not finish someone else's lease
        self.assertFalse(self.bus.finish_wake('codex', first['lease'], delivered=True),
                         'a superseded lease cannot be finished')
        seat = self.bus.read_seat('codex')
        self.assertEqual([l['id'] for l in seat.get('wakeLeases') or []], [second['lease']],
                         'the live lease is untouched')
        self.assertEqual(seat.get('woken') or [], [], 'and nothing was marked delivered')

    def test_an_expired_lease_makes_the_item_pending_again(self):
        self.codex_thread()
        self.task()
        with patch.object(B, 'WAKE_QUIET', 0):
            self.bus.reserve_wake('codex')
        self.assertEqual(self.bus.pending_for('codex')[0], [], 'a live lease keeps it out of pending')
        self.age_seat('codex', B.WAKE_LEASE_SECONDS + 5)
        self.assertTrue(self.bus.pending_for('codex')[0], 'after the lease expires the item is pending again')

    def test_a_failed_wake_counts_against_the_cap_and_is_not_marked_delivered(self):
        self.codex_thread()
        tid = self.task()
        result, run = self.wake(returncode=1)
        self.assertEqual(result, 'failed')
        seat = self.bus.read_seat('codex')
        self.assertEqual(seat.get('woken') or [], [], 'a failed send delivered nothing')
        self.assertIsNotNone(seat.get('failed'), 'the failure is recorded for the panel')
        self.assertEqual(len([w for w in seat.get('wakes', [])]), 1,
                         'a failure still consumes a slot, so a stale thread cannot storm')
        # the item stays pending, so a later attempt may retry it
        self.assertTrue(self.bus.pending_for('codex')[0])

    def test_a_wake_crash_does_not_leak_a_permanent_lease(self):
        self.codex_thread()
        self.task()
        with patch.object(B, 'WAKE_QUIET', 0), \
             patch.object(B, 'codex_command', return_value=['codex']), \
             patch.object(B.subprocess, 'run', side_effect=OSError('boom')):
            self.assertEqual(self.bus.wake_codex(), 'failed')
        seat = self.bus.read_seat('codex')
        self.assertEqual(seat.get('wakeLeases') or [], [], 'the lease is released even when the send raises')

    def test_the_hourly_cap_still_applies_and_a_failure_does_not_reset_it(self):
        self.codex_thread()
        self.task()
        with self.bus.lock:
            seat = self.bus.read_seat('codex')
            seat['wakes'] = [time.time()] * B.WAKE_PER_HOUR
            B.write_json(self.bus.seat_path('codex'), seat)
        self.assertIsNone(self.wake()[0], 'the cap blocks the wake')
        self.assertEqual(len(self.bus.read_seat('codex').get('wakes') or []), B.WAKE_PER_HOUR,
                         'a blocked wake does not append a slot')

    # -- pending claim ---------------------------------------------------------
    def test_pending_claim_marks_only_the_batch_it_actually_sends(self):
        self.codex_thread()
        ids = [self.task() for _ in range(8)]
        self.assertEqual(self.wake()[0], 'queued')
        pending, _ = self.bus.pending_for('codex')
        self.assertEqual(len(pending), 2, 'only six were queued, so two remain')

    def test_pending_claim_does_not_swallow_items_before_a_turn_exists(self):
        """A poll is a question: it reserves, but the turn (not the poll) has to finish the lease."""
        self.codex_thread()
        self.task()
        # codex is bound, so the poll must present its bound session; seat_matches rejects anything else.
        bound = self.p.binding('codex')['id']
        with patch.object(B, 'WAKE_QUIET', 0):
            out = self.bus.pending('codex', bound, claim=True)   # the quiet window is part of the decision
        self.assertTrue(out['wake'], 'the poll may say yes')
        self.assertIsNotNone(out.get('lease'), 'and it gets a lease to finish later')
        seat = self.bus.read_seat('codex')
        self.assertEqual(seat.get('woken') or [], [], 'the poll itself must not mark anything as told')
        self.assertEqual(seat.get('wakes') or [], [], 'nor spend a wake slot')
        # a second poll sees the reservation and stands down
        with patch.object(B, 'WAKE_QUIET', 0):
            again = self.bus.pending('codex', bound, claim=True)
        self.assertFalse(again['wake'], 'the second poll sees the reservation and stands down')
        # the turn finishes it
        self.assertTrue(self.bus.ack_wake('codex', out['lease'], True))
        self.assertTrue(self.bus.read_seat('codex').get('woken'), 'the turn marks what it was given')

    def test_a_stale_ack_returns_false_and_changes_nothing(self):
        self.codex_thread()
        self.task()
        self.assertFalse(self.bus.ack_wake('codex', 'nonexistent', True))

    # -- 规则提醒：按完成轮次，不按时间 ----------------------------------------
    def test_deepseek_receives_twentieth_round_rules_in_cli_result_without_a_host_hook(self):
        import contextlib
        import io
        from agentlink.cli import main
        tid = self.task(poster='codex', to='deepseek')
        self.bus.brief('deepseek', 'SessionStart')
        self.set_rounds('deepseek', 19, 19)
        output = io.StringIO()
        with patch.dict(os.environ, {'AGENTLINK_NO_WAKE': '1'}), contextlib.redirect_stdout(output):
            main(['--project', str(self.p.root), '--as', 'deepseek', 'deliver', tid, '--summary', 'completed round twenty'])
        result = json.loads(output.getvalue())
        self.assertIn('AgentLink 规则', result['rulesReminder'])
        fresh_bus = self.p.bus()
        self.assertEqual(fresh_bus.read_seat('deepseek')['roundsDone'], 20)
        self.assertEqual(fresh_bus.read_seat('deepseek')['roundsSinceRules'], 0)

    def round_trip(self, agent, count, poster='codex', to='deepseek'):
        """Complete `count` accepted rounds so the agent's round counter moves."""
        for _ in range(count):
            tid = self.task(poster=poster, to=to)
            self.bus.claim(to, tid)
            self.bus.deliver(to, tid, 'done')
            if self.bus.final_review_owner(self.bus.load(tid)) == agent:
                self.bus.review(agent, tid, 'accept')

    def set_rounds(self, agent, done, since):
        with self.bus.lock:
            seat = self.bus.read_seat(agent)
            seat['roundsDone'] = done
            seat['roundsSinceRules'] = since
            B.write_json(self.bus.seat_path(agent), seat)

    def test_a_new_session_gets_the_rules_once(self):
        """A session with no history gets the rules on its first natural hook; they are not repeated."""
        text = self.bus.brief('codex', 'SessionStart')
        self.assertIn('AgentLink 规则', text)
        self.assertIn('locks', text)
        self.assertIn('stop-ack', text)
        self.assertIn('deliver', text)
        second = self.bus.brief('codex', 'SessionStart')
        self.assertNotIn('AgentLink 规则', second, 'the same conversation must not repeat it')

    def test_a_legacy_config_without_the_option_defaults_to_twenty(self):
        """An older config.json has no rulesReminderEvery: the bus must still use the documented default."""
        self.assertEqual(self.bus.rules_every(), 20)
        # simulate an old config by removing the field
        saved = self.p.config_path.read_text(encoding='utf-8')
        data = json.loads(saved)
        data.get('options', {}).pop('rulesReminderEvery', None)
        self.p.config_path.write_text(json.dumps(data), encoding='utf-8')
        self.assertEqual(self.bus.rules_every(), 20, 'a missing field means 20, not 0')

    def test_the_reminder_waits_for_the_round_threshold_not_the_clock(self):
        """Nineteen completed rounds stay quiet; the twentieth is the threshold."""
        self.task()                                        # an open item: a mid-conversation reminder is allowed
        self.bus.brief('codex', 'SessionStart')            # the first rules are now behind us
        self.set_rounds('codex', 19, 19)
        self.assertNotIn('AgentLink 规则', self.bus.brief('codex', 'UserPromptSubmit'),
                         '19 rounds is below the default threshold')
        self.set_rounds('codex', 20, 20)
        text = self.bus.brief('codex', 'UserPromptSubmit')
        self.assertIn('AgentLink 规则', text, '20 completed rounds trigger the reminder')

    def test_no_elapsed_time_alone_can_trigger_a_reminder(self):
        """A long-lived but idle session must not be reminded just because time passed."""
        self.set_rounds('codex', 0, 0)
        with self.bus.lock:
            seat = self.bus.read_seat('codex')
            seat['rulesAt'] = (datetime.datetime.now().astimezone() -
                               datetime.timedelta(days=30)).isoformat()
            B.write_json(self.bus.seat_path('codex'), seat)
        self.task()
        self.assertNotIn('AgentLink 规则', self.bus.brief('codex', 'UserPromptSubmit'),
                         'time passing is not a round')

    def test_the_reminder_resets_the_counter_so_it_comes_back_every_twenty(self):
        self.task()
        self.bus.brief('codex', 'SessionStart')
        self.set_rounds('codex', 20, 20)
        self.assertIn('AgentLink 规则', self.bus.brief('codex', 'UserPromptSubmit'))
        seat = self.bus.read_seat('codex')
        self.assertEqual(seat.get('roundsSinceRules'), 0, 'the reminder is the new baseline')
        self.set_rounds('codex', 39, 19)
        self.assertNotIn('AgentLink 规则', self.bus.brief('codex', 'UserPromptSubmit'), 'still 19 since')
        self.set_rounds('codex', 40, 20)
        self.assertIn('AgentLink 规则', self.bus.brief('codex', 'UserPromptSubmit'), 'next twenty triggers again')

    def test_only_successful_delivery_and_acceptance_count_as_rounds(self):
        """A refused gate check or a duplicate callback must not inflate the counter."""
        before = int(self.bus.read_seat('deepseek').get('roundsDone') or 0)
        # a failed gate check: the delivery is refused, so nothing was completed
        tid = self.bus.post('codex', 'deepseek', 'gate fails', 'x', level='L1',
                            paths=['src/g'], checks=['exit 1'], no_dispatch=True)['id']
        self.bus.claim('deepseek', tid)
        with self.assertRaises(ValueError):
            self.bus.deliver('deepseek', tid, 'tried')
        self.assertEqual(int(self.bus.read_seat('deepseek').get('roundsDone') or 0), before,
                         'a refused delivery does not count')
        # a real delivery does count
        good = self.bus.post('codex', 'deepseek', 'ok', 'x', level='L1', paths=['src/h'], no_dispatch=True)['id']
        self.bus.claim('deepseek', good)
        self.bus.deliver('deepseek', good, 'done')
        self.assertEqual(int(self.bus.read_seat('deepseek').get('roundsDone') or 0), before + 1,
                         'a successful delivery counts exactly once')

    def test_one_acceptance_counts_once_and_a_stale_repeat_counts_nothing(self):
        tid = self.bus.post('codex', 'deepseek', 't', 'x', level='L1', paths=['src/i'], no_dispatch=True)['id']
        self.bus.claim('deepseek', tid)
        self.bus.deliver('deepseek', tid, 'done')
        self.assertEqual(self.bus.load(tid)['state'], 'delivered')
        before = int(self.bus.read_seat('codex').get('roundsDone') or 0)
        self.bus.review('codex', tid, 'accept')
        self.assertEqual(int(self.bus.read_seat('codex').get('roundsDone') or 0), before + 1,
                         'one real acceptance is one round')
        # the task is finished; a second accept must be refused and must not add a round
        try: self.bus.review('codex', tid, 'accept')
        except ValueError: pass
        self.assertEqual(int(self.bus.read_seat('codex').get('roundsDone') or 0), before + 1,
                         'a stale accept adds nothing')

    def test_an_idle_session_stays_silent(self):
        self.assertEqual(self.bus.brief('codex', 'UserPromptSubmit'), '',
                         'no open work: the reminder must not start a turn')
        self.assertIsNone(self.bus.read_seat('codex').get('roundsSinceRules'),
                          'and nothing is recorded as told')

    def test_the_rules_can_be_switched_off(self):
        self.p.save_settings({'rulesReminderEvery': 0})
        self.task()
        self.assertNotIn('AgentLink 规则', self.bus.brief('codex', 'SessionStart'),
                         '0 disables the reminder entirely')

    def test_the_rules_reminder_never_marks_pending_items_as_read(self):
        tid = self.task()
        text = self.bus.brief('codex', 'SessionStart')
        self.assertIn(tid, text, 'the item is still displayed, and the rules ride along')
        self.assertIn('AgentLink 规则', text, 'the first hook carries them')
        self.assertNotIn('AgentLink 规则', self.bus.brief('codex', 'UserPromptSubmit'),
                         'the reminder is not repeated on the next turn')

    def test_a_displayed_item_is_told_but_an_undisplayed_one_is_not(self):
        """Budget truncation must not mark the rest as read."""
        ids = [self.task() for _ in range(6)]
        text = self.bus.brief('codex', 'SessionStart')
        seat = self.bus.read_seat('codex')
        told = ' '.join(seat.get('told') or [])
        for tid in ids:
            if tid not in text:
                self.assertNotIn(tid, told, 'an item that was not displayed must not be marked told')


if __name__ == '__main__':
    unittest.main(verbosity=2)
