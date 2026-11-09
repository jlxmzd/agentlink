"""Final review recovery and scheduling controls. Only temporary projects and fake transports."""
import datetime
import os
from pathlib import Path
import sys
import tempfile
import os
import unittest
from contextlib import contextmanager
from unittest.mock import patch

SANDBOX = tempfile.TemporaryDirectory(prefix='agentlink-controls-')
os.environ['AGENTLINK_USER_HOME'] = str(Path(SANDBOX.name) / 'home')
# AGENTLINK_HOME outranks AGENTLINK_USER_HOME for projects.json: without this the suite could write to the
# developer's real registry. Scope both to this run's sandbox.
os.environ['AGENTLINK_HOME'] = str(Path(SANDBOX.name) / 'home' / '.agentlink')
os.environ['AGENTLINK_NO_WAKE'] = '1'
os.environ['AGENTLINK_NO_DISPATCH'] = '1'
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentlink import project, server, bus as B


class TaskControls(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=SANDBOX.name)
        self.addCleanup(self.temp.cleanup)
        self.p = project.Project(self.temp.name)
        self.p.apply({'codex': {'id': '00000000-0000-0000-0000-000000000001'},
                      'claude': {'id': '00000000-0000-0000-0000-000000000002'}},
                     options={'autoAccept': []})
        self.bus = self.p.bus()

    def task(self, poster='deepseek', to='codex'):
        allowed = ['src/task%d' % len(self.bus.all())]
        return self.bus.post(poster, to, 'task', 'spec', paths=allowed, no_dispatch=True)['id']

    def delivered(self, poster='deepseek', to='codex'):
        tid = self.task(poster, to)
        self.bus.deliver(to, tid, 'done')
        return tid

    def old(self, tid, **fields):
        t = self.bus.load(tid)
        stamp = (datetime.datetime.now().astimezone() - datetime.timedelta(hours=3)).isoformat()
        t.setdefault('finalReview', {}).update(waitingSince=stamp, lastReminderAt=stamp, **fields)
        self.bus.save(t)

    def test_deepseek_poster_routes_to_supported_lead(self):
        tid = self.delivered()
        t = self.bus.load(tid)
        self.assertEqual(self.bus.final_review_info(t)['owner'], 'codex')
        self.assertIn(tid, [t['id'] for t in self.bus.wake_items('codex').values()])
        self.bus.review('codex', tid, 'accept')
        self.assertEqual(self.bus.load(tid)['review']['by'], 'codex')

    def test_human_can_review_another_posters_delivery_with_audit(self):
        tid = self.delivered('claude', 'codex')
        self.bus.review('user', tid, 'accept', 'checked by human')
        self.assertEqual(self.bus.load(tid)['review']['by'], 'user')
        self.assertEqual(self.bus.load(tid)['history'][-1]['by'], 'user')

    def test_other_executor_cannot_approve_without_authority(self):
        tid = self.delivered('codex', 'deepseek')
        with self.assertRaises(ValueError): self.bus.review('claude', tid, 'accept')

    def test_timeout_reminders_are_bounded_and_never_accept(self):
        tid = self.delivered()
        self.bus.brief('codex', 'UserPromptSubmit')
        self.assertNotIn(tid, self.bus.pending_for('codex')[0])
        for n in range(B.FINAL_REVIEW_REMINDERS + 2):
            self.old(tid)
            self.bus.pump_final_review(tid)
            t = self.bus.load(tid)
            self.assertEqual(t['state'], 'delivered')
            self.assertEqual(t['finalReview']['reminders'], min(n + 1, B.FINAL_REVIEW_REMINDERS))
        self.assertIn(tid, self.bus.pending_for('codex')[0])

    def test_reminders_wait_between_attempts(self):
        tid = self.delivered()
        self.old(tid)
        self.bus.pump_final_review(tid)
        self.bus.pump_final_review(tid)
        self.assertEqual(self.bus.load(tid)['finalReview']['reminders'], 1)

    def test_human_inbox_does_not_wake_a_model(self):
        tid = self.delivered('user', 'codex')
        self.old(tid)
        self.bus.pump_final_review(tid)
        self.assertEqual(self.bus.final_review_info(self.bus.load(tid))['owner'], 'user')
        self.assertFalse(self.bus.wake_items('codex'))

    def test_offline_reviewer_falls_back_to_lead_after_timeout(self):
        tid = self.delivered('claude', 'codex')
        self.old(tid)
        self.bus.pump_final_review(tid)
        self.assertEqual(self.bus.final_review_info(self.bus.load(tid))['owner'], 'codex')

    def test_manual_takeover_preserves_delivery_and_changes_reviewer(self):
        tid = self.delivered('user', 'codex')
        self.bus.review_attention('user', tid, 'takeover')
        t = self.bus.load(tid)
        self.assertEqual((t['state'], t['from'], t['finalReview']['owner']), ('delivered', 'user', 'codex'))
        self.assertTrue(self.bus.wake_items('codex'))

    def test_remind_is_a_new_notification_with_cooldown(self):
        tid = self.delivered()
        self.bus.brief('codex', 'UserPromptSubmit')
        self.bus.review_attention('user', tid, 'remind')
        self.assertIn(tid, self.bus.pending_for('codex')[0])
        with self.assertRaises(ValueError): self.bus.review_attention('user', tid, 'remind')

    def test_paused_task_keeps_locks_and_refuses_delivery_dispatch_and_claim(self):
        tid = self.task()
        self.bus.claim('codex', tid)
        self.bus.control('user', tid, 'pause')
        self.assertIn(tid, self.bus.locks())
        self.assertEqual(self.bus.task_actions(self.bus.load(tid)), ['resume', 'cancel'])
        with self.assertRaises(ValueError): self.bus.deliver('codex', tid, 'late')
        with self.assertRaises(ValueError): self.bus.release(tid)
        with self.assertRaises(ValueError): self.bus.claim('codex', tid)
        self.assertEqual(self.bus.dispatch(tid), 'skipped')
        self.bus.control('user', tid, 'resume')
        self.assertEqual(self.bus.load(tid)['state'], 'claimed')
        self.bus.deliver('codex', tid, 'resumed')

    def test_cancel_paused_task_refuses_resume_and_releases_lock(self):
        tid = self.task()
        self.bus.control('user', tid, 'pause')
        self.bus.review('user', tid, 'cancel')
        with self.assertRaises(ValueError): self.bus.control('user', tid, 'resume')
        self.assertNotIn(tid, self.bus.locks())
        self.assertEqual(self.bus.task_actions(self.bus.load(tid)), [])

    def test_pause_suppresses_old_stall_notifications(self):
        tid = self.task('codex', 'deepseek')
        self.bus.mark(tid, state='accepted', stalled=True)
        self.bus.control('user', tid, 'pause')
        self.assertFalse(self.bus.wake_items('codex'))
        self.assertFalse(self.bus.context_items('codex'))

    def test_dispatch_cancelled_during_preflight_never_sends(self):
        from agentlink import harness
        tid = self.task('codex', 'deepseek')
        def preflight(*args):
            self.bus.control('user', tid, 'pause')
            return {'sessionId': 'fake', 'running': False}
        with patch.dict(os.environ, {'AGENTLINK_NO_DISPATCH': ''}), \
             patch.object(self.bus, 'bound', return_value='fake'), patch.object(harness, 'Client') as client, \
             patch.object(harness, 'find_session', side_effect=preflight), \
             patch.object(harness, 'archived_sessions', return_value=set()):
            self.assertEqual(self.bus.dispatch(tid), 'skipped')
            client.return_value.call.assert_not_called()
        self.assertEqual(self.bus.load(tid)['state'], 'paused')

    def test_late_dispatch_result_cannot_overwrite_paused_state(self):
        from agentlink import harness
        tid = self.task('codex', 'deepseek')
        def send(*args):
            self.bus.control('user', tid, 'pause')
            return {'accepted': True}
        with patch.dict(os.environ, {'AGENTLINK_NO_DISPATCH': ''}), \
             patch.object(self.bus, 'bound', return_value='fake'), patch.object(harness, 'Client') as client, \
             patch.object(harness, 'find_session', return_value={'running': False}), \
             patch.object(harness, 'archived_sessions', return_value=set()):
            client.return_value.call.side_effect = send
            self.assertEqual(self.bus.dispatch(tid), 'skipped')
        self.assertEqual(self.bus.load(tid)['state'], 'paused')

    def test_takeback_is_claimed_instead_of_cancelled(self):
        tid = self.task('claude', 'codex')
        self.bus.reassign('claude', tid, 'claude', no_dispatch=True)
        self.assertEqual(self.bus.load(tid)['state'], 'claimed')
        self.assertIn(tid, self.bus.locks())

    def test_claimed_takeback_waits_for_original_executor_and_keeps_locks(self):
        tid = self.task('codex', 'claude')
        self.bus.claim('claude', tid)
        result = self.bus.reassign('codex', tid, 'codex', 'stop and transfer', no_dispatch=True)
        self.assertEqual((result['state'], result['to']), ('recalling', 'claude'))
        self.assertIn(tid, self.bus.locks())
        self.assertTrue(self.bus.wake_items('claude'))
        self.assertIn('stop-ack', self.bus.brief('claude', 'UserPromptSubmit'))
        for fn in (lambda: self.bus.claim('codex', tid), lambda: self.bus.deliver('claude', tid, 'late'),
                   lambda: self.bus.control('user', tid, 'pause'), lambda: self.bus.review('user', tid, 'cancel'),
                   lambda: self.bus.reassign('user', tid, 'deepseek', no_dispatch=True)):
            with self.assertRaises(ValueError): fn()
        r = self.bus.load(tid)['recall']
        with self.assertRaises(ValueError): self.bus.acknowledge_stop('codex', tid, r['token'], 'stopped')
        with self.assertRaises(ValueError): self.bus.acknowledge_stop('claude', tid, 'wrong', 'stopped')
        with self.assertRaises(ValueError): self.bus.acknowledge_stop('claude', tid, r['token'], '')
        self.bus.acknowledge_stop('claude', tid, r['token'], 'all writes, tools and children stopped', no_dispatch=True)
        t = self.bus.load(tid)
        self.assertEqual((t['state'], t['to']), ('posted', 'codex'))
        self.assertEqual(t['recall']['confirmedBy'], 'claude')
        self.assertNotIn('token', t['recall'])
        with self.assertRaises(ValueError): self.bus.deliver('claude', tid, 'stale')
        with self.assertRaises(ValueError): self.bus.acknowledge_stop('claude', tid, r.get('token', ''), 'duplicate')
        self.bus.claim('codex', tid)

    def test_unclaimed_takeback_does_not_notify_or_wait(self):
        tid = self.task('codex', 'deepseek')
        with patch.object(self.bus, 'notify_recall') as notify:
            self.bus.reassign('codex', tid, 'codex', no_dispatch=True)
            notify.assert_not_called()
        self.assertEqual(self.bus.load(tid)['state'], 'claimed')
        self.assertNotIn('recall', self.bus.load(tid))
        with self.assertRaises(ValueError): self.bus.claim('deepseek', tid)

    def test_cancel_active_task_requires_stop_ack_before_releasing_locks(self):
        tid = self.task('codex', 'claude')
        self.bus.claim('claude', tid)
        self.bus.review('user', tid, 'cancel', 'terminate')
        t = self.bus.load(tid)
        self.assertEqual(t['state'], 'recalling')
        self.assertEqual(t['recall']['intent'], 'cancel')
        self.assertIn(tid, self.bus.locks())
        self.bus.acknowledge_stop('claude', tid, t['recall']['token'], 'all work stopped', no_dispatch=True)
        self.assertEqual(self.bus.load(tid)['state'], 'cancelled')
        self.assertNotIn(tid, self.bus.locks())

    def test_recall_token_never_reaches_panel_or_flow_snapshot(self):
        tid = self.task('codex', 'claude')
        self.bus.claim('claude', tid)
        self.bus.reassign('codex', tid, 'codex', no_dispatch=True)
        token = self.bus.load(tid)['recall']['token']
        import json
        self.assertNotIn(token, json.dumps(server.tasks_of(self.p)))
        with patch.object(project, 'projects', return_value=[self.p]): snap = server.flow_snapshot()
        self.assertNotIn(token, json.dumps(snap))
        self.assertEqual(snap['tasks'][0]['actions'], [])

    def test_deepseek_recall_queues_stop_message_with_stable_request_id(self):
        from agentlink import harness
        tid = self.task('codex', 'deepseek')
        self.bus.claim('deepseek', tid)
        self.bus.reassign('codex', tid, 'codex', no_dispatch=True)
        with patch.dict(os.environ, {'AGENTLINK_NO_DISPATCH': ''}), \
             patch.object(self.bus, 'bound', return_value='fake'), patch.object(harness, 'Client') as client:
            client.return_value.call.return_value = {'accepted': True}
            self.assertEqual(self.bus.notify_recall(tid), 'sent')
            request = client.return_value.call.call_args[0][1]
            self.assertEqual(request['mode'], 'queue')
            self.assertIn('stop-ack', request['content'][0]['text'])
            self.assertEqual(self.bus.notify_recall(tid), 'sent')
            self.assertEqual(client.return_value.call.call_count, 1)
        self.assertEqual(self.bus.load(tid)['state'], 'recalling')

    def test_paused_ai_review_preserves_running_reviewer_on_resume(self):
        tid = self.task('codex', 'deepseek')
        t = self.bus.load(tid)
        t.update(state='ai_review', aiReview={'status': 'started', 'token': 'old', 'attempt': 0,
                                            'startedAt': B.now(), 'attempts': 2})
        self.bus.save(t)
        self.bus.control('user', tid, 'pause')
        with self.assertRaises(ValueError): self.bus.ai_review('deepseek', tid, 0, 'old', 'accept')
        self.bus.control('user', tid, 'resume')
        t = self.bus.load(tid)
        self.assertEqual(t['aiReview']['token'], 'old')
        self.assertIsNotNone(t['aiReview']['startedAt'])
        self.assertEqual(t['aiReview']['attempts'], 2)
        self.bus.ai_review('deepseek', tid, 0, 'old', 'accept')
        self.assertEqual(self.bus.load(tid)['state'], 'delivered')

    def test_direct_takeback_clears_unclaimed_escalation_before_next_recall(self):
        tid = self.task('codex', 'claude')
        t = self.bus.load(tid)
        t['escalation'] = {'to': 'claude', 'at': B.now()}
        t['escalationClaimed'] = False
        self.bus.save(t)
        self.bus.reassign('codex', tid, 'codex', no_dispatch=True)
        self.assertNotIn('escalation', self.bus.load(tid))
        self.bus.reassign('user', tid, 'claude', no_dispatch=True)
        self.assertEqual(self.bus.load(tid)['state'], 'recalling')

    def test_recall_notification_failure_never_transfers_or_unlocks(self):
        from agentlink import harness
        tid = self.task('codex', 'deepseek')
        self.bus.claim('deepseek', tid)
        self.bus.reassign('codex', tid, 'codex', no_dispatch=True)
        with patch.dict(os.environ, {'AGENTLINK_NO_DISPATCH': ''}), \
             patch.object(self.bus, 'bound', return_value='fake'), patch.object(harness, 'Client') as client:
            client.return_value.call.side_effect = ValueError('offline')
            self.assertEqual(self.bus.notify_recall(tid), 'uncertain')
        self.assertEqual((self.bus.load(tid)['state'], self.bus.load(tid)['to']), ('recalling', 'deepseek'))
        self.assertIn(tid, self.bus.locks())

    def test_cli_stop_ack_and_stale_token_across_recall_generations(self):
        import contextlib
        import io
        from agentlink.cli import main
        tid = self.task('codex', 'claude')
        self.bus.claim('claude', tid)
        self.bus.reassign('codex', tid, 'codex', no_dispatch=True)
        first = self.bus.load(tid)['recall']['token']
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['--project', str(self.p.root), '--as', 'claude', 'stop-ack', tid,
                                   '--token', first, '--note', 'tools and writes stopped', '--no-dispatch']), 0)
        self.bus.claim('codex', tid)
        self.bus.reassign('user', tid, 'claude', no_dispatch=True)
        second = self.bus.load(tid)['recall']['token']
        self.assertNotEqual(first, second)
        with self.assertRaises(ValueError): self.bus.acknowledge_stop('codex', tid, first, 'stopped')
        self.assertEqual(self.bus.load(tid)['state'], 'recalling')
        self.assertIn(tid, self.bus.locks())
        self.bus.acknowledge_stop('codex', tid, second, 'all stopped', no_dispatch=True)
        self.assertEqual((self.bus.load(tid)['state'], self.bus.load(tid)['to']), ('posted', 'claude'))

    def test_wait_timeout_preserves_real_state_including_review_and_recall(self):
        tid = self.task('codex', 'claude')
        for state in ('posted', 'claimed', 'rework', 'ai_review', 'recalling'):
            t = self.bus.load(tid); t['state'] = state; self.bus.save(t)
            result = self.bus.wait(tid, timeout=0)
            self.assertEqual(result['state'], state)
            self.assertTrue(result['timedOut'])
            self.assertEqual(self.bus.load(tid)['state'], state)
        for state in ('paused', 'blocked', 'delivered', 'accepted', 'cancelled'):
            t = self.bus.load(tid); t['state'] = state; self.bus.save(t)
            result = self.bus.wait(tid, timeout=0)
            self.assertEqual(result['state'], state)
            self.assertFalse(result['timedOut'])

    def test_brief_budget_does_not_mark_undisplayed_items_read(self):
        ids = []
        for n in range(5):
            tid = self.task('codex', 'claude')
            t = self.bus.load(tid); t['title'] = str(n) + 'x' * 200; self.bus.save(t)
            ids.append(tid)
        with patch.object(self.bus, 'detail', side_effect=lambda agent, t, why: t['id'] + 'x' * 1700):
            first = self.bus.brief('claude', 'UserPromptSubmit')
            pending = self.bus.pending_for('claude')[0]
        self.assertGreater(len(pending), 0)
        self.assertTrue(all(tid not in first for tid in pending))

    def test_wake_batch_marks_only_queued_six_tasks(self):
        ids = [self.task('claude', 'codex') for _ in range(8)]
        with patch.object(self.bus, 'wake_enabled', return_value=True), patch.object(B, 'WAKE_QUIET', 0), \
             patch.object(B, 'codex_command', return_value=['codex']), \
             patch.object(B.subprocess, 'run') as run:
            run.return_value.returncode = 0
            self.assertEqual(self.bus.wake_codex(), 'queued')
        self.assertEqual(len(self.bus.pending_for('codex')[0]), 2)

    def test_api_returns_controls_and_validates_state_again(self):
        tid = self.task()
        body = {'root': str(self.p.root), 'id': tid}
        server.api('POST', '/api/task/act', {}, dict(body, action='pause'))
        data = server.tasks_of(self.p)
        self.assertEqual(data['tasks'][0]['actions'], ['resume', 'cancel'])
        with self.assertRaises(ValueError): server.api('POST', '/api/task/act', {}, dict(body, action='accept'))
        server.api('POST', '/api/task/act', {}, dict(body, action='resume'))
        self.bus.deliver('codex', tid, 'done')
        with patch.object(project, 'projects', return_value=[self.p]): snap = server.flow_snapshot()
        self.assertEqual(snap['tasks'][0]['finalReview']['owner'], 'codex')
        self.assertIn('accept', snap['tasks'][0]['actions'])
        server.api('POST', '/api/task/act', {}, dict(body, action='accept'))
        self.assertEqual(self.bus.load(tid)['state'], 'accepted')

    def test_rules_reminder_interval_is_validated_saved_and_portable(self):
        self.assertEqual(self.p.option('rulesReminderEvery'), 20)
        for invalid in (-1, 201, True, '20', 2.5):
            with self.assertRaises(ValueError): self.p.save_settings({'rulesReminderEvery': invalid})
        self.p.save_settings({'rulesReminderEvery': 0})
        self.assertEqual(self.p.option('rulesReminderEvery'), 0)
        self.p.save_settings({'rulesReminderEvery': 20})
        package = self.p.migration_export()
        self.assertEqual(package['options']['rulesReminderEvery'], 20)


class HandoverDispatchIdentity(unittest.TestCase):
    """A task that comes back to DeepSeek must be a NEW message, not a deduped repeat of an old one.

    The bridge dedupes by requestId and answers `accepted` even when it drops the prompt, which is exactly why
    the user saw "no new notification" after a hand-over. Every execution round therefore needs its own identity.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=SANDBOX.name)
        self.addCleanup(self.temp.cleanup)
        self.p = project.Project(self.temp.name)
        self.p.apply({'codex': {'id': '00000000-0000-0000-0000-000000000001'},
                      'claude': {'id': '00000000-0000-0000-0000-000000000002'},
                      'deepseek': {'id': 'session-ds'}},
                     options={'autoAccept': []})
        self.bus = self.p.bus()
        self.bridge = DedupeBridge()

    def task(self, poster='codex', to='deepseek'):
        allowed = ['src/task%d' % len(self.bus.all())]
        return self.bus.post(poster, to, 'task', 'spec', paths=allowed, no_dispatch=True)['id']

    @contextmanager
    def transport(self, find=None, bound=None):
        """Keep the fake transport active across dispatch AND manual retries."""
        from agentlink import harness
        with patch.dict(os.environ, {'AGENTLINK_NO_DISPATCH': ''}), \
             patch.object(self.bus, 'bound', side_effect=bound or (lambda role: 'session-ds')), \
             patch.object(harness, 'Client', return_value=self.bridge), \
             patch.object(harness, 'find_session', side_effect=find or (lambda client, sid: {'running': False})), \
             patch.object(harness, 'archived_sessions', return_value=set()):
            yield

    def send(self, tid):
        """Dispatch through a fake Harness that dedupes by requestId, exactly like the real bridge."""
        with self.transport():
            return self.bus.dispatch(tid)

    def generation(self, tid):
        return self.bus.dispatch_generation(self.bus.load(tid))

    # -- the reported bug ----------------------------------------------------
    def test_ds_then_codex_then_ds_sends_a_new_real_message(self):
        tid = self.task()
        self.assertEqual(self.send(tid), 'accepted')
        self.assertEqual(len(self.bridge.delivered), 1, 'the first send really delivered')
        # DS never claimed it, so Codex can take it directly. Codex then confirms its own stop before handing back.
        self.bus.reassign('codex', tid, 'codex', 'take it back', no_dispatch=True)
        self.assertEqual(self.bus.load(tid)['state'], 'claimed')
        self.assertNotIn('recall', self.bus.load(tid), 'unclaimed DS work needs no stop notification')
        self.bus.reassign('codex', tid, 'deepseek', 'hand it back', no_dispatch=True)
        recall = self.bus.load(tid)['recall']
        self.bus.acknowledge_stop('codex', tid, recall['token'], 'no tools or writes remain', no_dispatch=True)
        self.assertEqual(self.bus.load(tid)['state'], 'posted')
        self.assertEqual(self.generation(tid), 2, 'each hand-over starts a new execution round')
        self.assertEqual(self.send(tid), 'accepted')
        self.assertEqual(len(self.bridge.delivered), 2,
                         'the second round must produce a NEW real message, not a deduped repeat')
        self.assertNotEqual(self.bridge.delivered[0], self.bridge.delivered[1], 'ids differ across rounds')

    def test_a_claimed_task_is_recalled_then_stop_acked_before_the_new_round(self):
        tid = self.task()
        self.bus.claim('deepseek', tid)
        out = self.bus.reassign('codex', tid, 'codex', 'running: ask it to stop', no_dispatch=True)
        self.assertEqual(out['state'], 'recalling', 'a claimed task is never taken by force')
        token = self.bus.load(tid)['recall']['token']
        ack = self.bus.acknowledge_stop('deepseek', tid, token, '已停止全部写入、工具及子智能体', no_dispatch=True)
        self.assertEqual(ack['state'], 'posted', 'the ack hands the task to its new owner, who must still claim it')
        self.assertEqual(self.generation(tid), 1, 'a completed hand-over is one new round')
        self.bus.reassign('codex', tid, 'deepseek', 'back to DS', no_dispatch=True)
        self.assertEqual(self.generation(tid), 2)
        self.assertEqual(self.send(tid), 'accepted')
        self.assertEqual(len(self.bridge.delivered), 1, 'a brand-new round delivers once and for real')
        self.assertEqual(self.bus.load(tid)['to'], 'deepseek')

    def test_the_old_round_identity_is_not_resurrected(self):
        tid = self.task()
        request_one = self.bus.dispatch_request(self.bus.load(tid), 0)
        self.send(tid)
        self.bus.reassign('codex', tid, 'claude', 'away', no_dispatch=True)
        self.bus.reassign('codex', tid, 'deepseek', 'again', no_dispatch=True)
        request_two = self.bus.dispatch_request(self.bus.load(tid), 0)
        self.assertNotEqual(request_one, request_two)

    # -- stable inside one round ----------------------------------------------
    def test_retries_inside_one_round_keep_the_same_request_id(self):
        tid = self.task()
        request = self.bus.dispatch_request(self.bus.load(tid), 0)
        self.assertEqual(self.bus.dispatch_request(self.bus.load(tid), 0), request, 'idempotent recompute')
        self.bridge.accepted = False                       # delivery happened, but acknowledgement was uncertain
        with self.transport():
            self.assertEqual(self.bus.dispatch(tid), 'uncertain')
            self.assertEqual(self.bus.load(tid)['dispatch']['requestId'], request)
            self.assertEqual(self.bus.release(tid)['dispatch'], 'accepted')
        self.assertEqual(self.bus.load(tid)['dispatch']['requestId'], request,
                         'a manual retry inside one round must not mint a new id')
        self.assertEqual(self.bridge.calls, [request, request], 'the real retry RPC reuses the same id')
        self.assertEqual(len(self.bridge.delivered), 1, 'the bridge produces only one actual message')

    def test_binding_changed_during_preflight_never_receives_an_old_prompt(self):
        tid = self.task(); target = ['session-ds']
        def find(client, sid):
            target[0] = 'new-session'
            return {'running': False}
        with self.transport(find=find, bound=lambda role: target[0]):
            self.assertEqual(self.bus.dispatch(tid), 'skipped')
        self.assertEqual(self.bridge.calls, [])

    def test_a_late_failed_preflight_preserves_a_competing_success(self):
        tid = self.task(); depth = [0]
        def find(client, sid):
            depth[0] += 1
            if depth[0] == 1:
                self.assertEqual(self.bus.dispatch(tid), 'accepted')
                raise ValueError('busy from an older check')
            return {'running': False}
        with self.transport(find=find):
            self.assertEqual(self.bus.dispatch(tid), 'accepted')
        self.assertEqual(self.bus.load(tid)['dispatch']['state'], 'accepted')
        self.assertEqual(len(self.bridge.calls), 1)

    def test_handover_during_preflight_discards_the_old_send(self):
        tid = self.task()
        def find(client, sid):
            self.bus.reassign('codex', tid, 'claude', no_dispatch=True)
            self.bus.reassign('codex', tid, 'deepseek', no_dispatch=True)
            return {'running': False}
        with self.transport(find=find):
            self.assertEqual(self.bus.dispatch(tid), 'skipped')
        self.assertEqual(self.bridge.calls, [])
        self.assertEqual(self.send(tid), 'accepted')

    def test_late_rpc_completion_cannot_replace_the_new_round(self):
        tid = self.task(); original = self.bridge.call; old_request = [None]
        def call(method, payload):
            result = original(method, payload)
            if old_request[0] is None:
                old_request[0] = payload['requestId']
                self.bus.reassign('codex', tid, 'claude', no_dispatch=True)
                self.bus.reassign('codex', tid, 'deepseek', no_dispatch=True)
                self.assertEqual(self.bus.dispatch(tid), 'accepted')
            return result
        with self.transport(), patch.object(self.bridge, 'call', side_effect=call):
            self.assertEqual(self.bus.dispatch(tid), 'skipped')
        current = self.bus.load(tid)['dispatch']
        self.assertEqual(current['state'], 'accepted')
        self.assertNotEqual(current['requestId'], old_request[0])

    def test_binding_changed_during_rpc_cannot_claim_delivery_to_the_new_binding(self):
        tid = self.task(); target = ['session-ds']; original = self.bridge.call
        def call(method, payload):
            result = original(method, payload)
            target[0] = 'new-session'
            return result
        with self.transport(bound=lambda role: target[0]), patch.object(self.bridge, 'call', side_effect=call):
            self.assertEqual(self.bus.dispatch(tid), 'skipped')
            self.assertEqual(self.bus.load(tid)['dispatch']['state'], 'uncertain')
            self.assertEqual(self.bus.dispatch(tid), 'skipped', 'a changed binding requires an explicit handover')
        self.assertEqual(len(self.bridge.calls), 1)

    def test_a_late_manual_retry_cannot_reset_a_new_round(self):
        tid = self.task(); self.bridge.accepted = False
        with self.transport():
            self.assertEqual(self.bus.dispatch(tid), 'uncertain')
        load = self.bus.load; first = [True]
        def raced_load(task_id):
            t = load(task_id)
            if first[0]:
                first[0] = False
                self.bus.reassign('codex', tid, 'claude', no_dispatch=True)
                self.bus.reassign('codex', tid, 'deepseek', no_dispatch=True)
                self.bridge.accepted = True
                self.assertEqual(self.bus.dispatch(tid), 'accepted')
            return t
        with self.transport(), patch.object(self.bus, 'load', side_effect=raced_load):
            self.assertEqual(self.bus.release(tid)['dispatch'], 'skipped')
        self.assertEqual(load(tid)['dispatch']['state'], 'accepted')
        self.assertEqual(len(self.bridge.calls), 2, 'only the old and new rounds sent, never the stale retry')

    def test_a_duplicate_dispatch_sends_only_once(self):
        tid = self.task()
        self.send(tid)
        calls = len(self.bridge.calls)
        self.send(tid)
        self.assertEqual(len(self.bridge.calls), calls, 'the accepted state short-circuits the repeat')

    def test_pause_and_resume_do_not_start_a_new_round(self):
        tid = self.task()
        gen = self.generation(tid)
        self.bus.control('user', tid, 'pause')
        self.bus.control('user', tid, 'resume')
        self.assertEqual(self.generation(tid), gen, 'pause/resume must not mint a new round')

    def test_a_rework_is_a_new_attempt_within_the_same_round(self):
        tid = self.task()
        self.bus.claim('deepseek', tid)
        self.bus.deliver('deepseek', tid, 'done')
        self.bus.review('codex', tid, 'reject', 'bad', kind='code')
        t = self.bus.load(tid)
        self.assertEqual(self.generation(tid), 0, 'a rework does not change hands')
        self.assertNotEqual(self.bus.dispatch_request(t, 0), self.bus.dispatch_request(t, t['rejects']))

    # -- stale answers across a hand-over -----------------------------------
    def test_a_late_result_from_the_old_round_cannot_overwrite_the_new_one(self):
        tid = self.task()
        old_request = self.bus.dispatch_request(self.bus.load(tid), 0)
        self.bus.reassign('codex', tid, 'claude', 'away', no_dispatch=True)
        self.bus.reassign('codex', tid, 'deepseek', 'again', no_dispatch=True)
        self.assertEqual(self.bus.load(tid)['to'], 'deepseek', 'the task is back with DeepSeek')
        wrote = self.bus.mark_identity(tid, dict(state='accepted', attempt=0), generation=0, attempt=0,
                                       requestId=old_request)
        self.assertIsNone(wrote, 'a stale round identity must not write')
        cur = self.bus.load(tid)
        self.assertEqual(cur.get('dispatch'), None, 'the new round has no dispatch record yet')
        self.assertEqual(self.generation(tid), 2)

    def test_a_failed_preflight_does_not_overwrite_the_new_receiver(self):
        tid = self.task()
        self.bus.reassign('codex', tid, 'codex', 'back', no_dispatch=True)
        wrote = self.bus.mark_identity(tid, dict(state='pending', attempt=0), generation=0, attempt=0, requestId='x')
        self.assertIsNone(wrote, 'a dispatch write must not land on a task that left DeepSeek')

    # -- legacy records ------------------------------------------------------
    def test_a_record_without_a_generation_keeps_its_old_request_id(self):
        import uuid
        tid = self.task()
        t = self.bus.load(tid)
        t.pop('dispatchGeneration', None)
        self.bus.save(t)
        legacy = self.bus.dispatch_request(self.bus.load(tid), 0)
        expected = str(uuid.uuid5(uuid.NAMESPACE_URL,
                                  '%s/agentlink/%s/%d' % (self.p.root.as_posix(), tid, 0)))
        self.assertEqual(legacy, expected, 'old records must stay byte-identical')

    # -- AI review identity --------------------------------------------------
    def test_ai_review_identity_changes_across_a_handover_but_not_across_a_retry(self):
        tid = self.task()
        before = self.bus.ai_review_request(self.bus.load(tid), 0)
        self.bus.reassign('codex', tid, 'claude', 'away', no_dispatch=True)
        self.bus.reassign('codex', tid, 'deepseek', 'again', no_dispatch=True)
        after = self.bus.ai_review_request(self.bus.load(tid), 0)
        self.assertNotEqual(before, after, 'the reviewer must not be asked with a stale id')
        t = self.bus.load(tid)
        self.assertEqual(self.bus.ai_review_request(t, 0), after, 'a retry keeps the same review id')

    def test_redelivery_after_handover_creates_a_fresh_ai_review_message(self):
        self.p.apply({}, options={'aiReviewEnabled': True, 'aiReviewSession': {'id': 'review-session', 'title': 'Review'}})
        tid = self.task()
        self.bus.claim('deepseek', tid)
        with self.transport():
            self.bus.deliver('deepseek', tid, 'first delivery')
        first = self.bus.load(tid)['aiReview']
        self.bus.reassign('codex', tid, 'codex', no_dispatch=True)
        self.bus.reassign('codex', tid, 'deepseek', no_dispatch=True)
        recall = self.bus.load(tid)['recall']
        self.bus.acknowledge_stop('codex', tid, recall['token'], 'all stopped', no_dispatch=True)
        self.bus.claim('deepseek', tid)
        with self.transport():
            self.bus.deliver('deepseek', tid, 'new delivery after handover')
        second = self.bus.load(tid)['aiReview']
        self.assertNotEqual(first['requestId'], second['requestId'])
        self.assertNotEqual(first['token'], second['token'])
        self.assertEqual(len(self.bridge.delivered), 2, 'the bridge delivers both distinct review rounds')


class DedupeBridge(object):
    """A fake Harness that dedupes by requestId, forever — the real bridge's behaviour in miniature."""

    def __init__(self):
        self.delivered = []
        self.calls = []
        self.accepted = True

    def call(self, method, payload):
        if method != 'session.prompt':
            return {'items': []}
        rid = payload.get('requestId')
        self.calls.append(rid)
        if rid in self.delivered:
            return {'accepted': True, 'deduped': True}      # accepted, but nothing new arrives
        self.delivered.append(rid)
        return {'accepted': self.accepted}


if __name__ == '__main__':
    unittest.main(verbosity=2)
