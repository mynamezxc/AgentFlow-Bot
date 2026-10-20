"""Durable inference budgets and loop brakes. Only owner actions can reset them."""
import hashlib
import json

from .store import DomainError, now

DEFAULT_BUDGET = {'run_turns': 24, 'project_turns': 64, 'project_runs': 24, 'project_tokens': 0}
BUDGET_RANGES = {'run_turns': (1, 500), 'project_turns': (1, 5000), 'project_runs': (1, 1000), 'project_tokens': (0, 10 ** 9)}
BUDGET_UNITS = {'run_turns': 'turns', 'project_turns': 'turns', 'project_runs': 'runs', 'project_tokens': 'tokens'}


class LoopStopped(DomainError):
    pass


class RunSafety:
    # Fallback defaults; the live values come from settings.budget.
    RUN_LIMIT = DEFAULT_BUDGET['run_turns']
    ROOT_LIMIT = DEFAULT_BUDGET['project_turns']
    FAMILY_LIMIT = DEFAULT_BUDGET['project_runs']

    def __init__(self, engine):
        self.engine, self.store = engine, engine.store
        engine.decision_handlers['safety'] = self.answer

    def budget(self):
        """settings.budget merged over the defaults; invalid entries fall back to the default."""
        raw = self.store.settings().get('budget')
        result = dict(DEFAULT_BUDGET)
        if isinstance(raw, dict):
            for key, (low, high) in BUDGET_RANGES.items():
                value = raw.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and low <= value <= high:
                    result[key] = value
        return result

    def root(self, run):
        return self.store.get('runs', run.get('root_id') or run['id'])

    def halted(self, run):
        root = self.root(run)
        return bool(root.get('safety_halted')) or root['status'] == 'cancelled'

    def halt(self, run, reason, kind='loop', budget=None):
        """Stop the whole workflow and ask the owner. `kind='budget'` may offer "allow for this task";
        loop brakes (repeated calls/output, text-only turns) only offer continue/cancel."""
        with self.store.transaction():
            root = self.root(run)
            if not root.get('safety_halted'):
                self.store.patch('runs', root['id'], {'safety_halted': True, 'safety_reason': reason,
                    'safety_kind': kind, 'safety_budget': budget})
                actor = self.store.get('agents', 'a-lead')
                approval = self.engine.enqueue('a-lead', 'Review the stopped workflow.', 'safety_approval',
                    project_id=run.get('project_id'), root_id=root['id'], channel='dm-a-lead',
                    status='waiting_user', system_managed=True)
                actions = self.actions(kind, budget)
                question = self.engine.team.ask(actor, {
                    'question': ('Work stopped: a budget was reached. Continue, allow this task to finish, or cancel?' if kind == 'budget'
                        else 'Work stopped by a loop brake: ' + reason + ' Continue or cancel?'),
                    'context': reason, 'options': [a['label'] for a in actions],
                    'actions': actions, 'action_field': 'safety_action', 'action_handler': 'safety',
                    'blocking': True}, approval, _routed=True)
                self.store.patch('decisions', question['decision_id'], {'safety_request': True, 'safety_root': root['id'],
                    'safety_kind': kind, 'safety_budget': budget})
            for member in self.store.all('runs'):
                if (member.get('root_id') == root['id'] and member['status'] not in ('done','failed','cancelled')
                        and not member.get('system_managed')):
                    if not member.get('safety_previous_status'):
                        self.store.patch('runs', member['id'], {'safety_previous_status': member['status'], 'status': 'waiting_user'})
                    self.engine.providers.cancel(member['id'])
        raise LoopStopped(reason, 409)

    def actions(self, kind, budget):
        if kind == 'budget':
            amount = self.budget().get(budget, 0)
            unit = BUDGET_UNITS.get(budget, 'turns')
            actions = [{'value': 'continue', 'label': f'Continue (+{amount:,} {unit})', 'style': 'primary',
                        'hint': 'Adds the same budget again for this project.'},
                       {'value': 'allow_task', 'label': "Allow for this task until it's done", 'style': 'quiet',
                        'hint': 'Turn, run and token budgets are skipped for this task. Loop brakes stay on.'}]
        else:
            actions = [{'value': 'continue', 'label': 'Continue', 'style': 'primary',
                        'hint': 'Resets the loop brake. More budget would not fix a loop, so there is no unlimited option.'}]
        return actions + [{'value': 'cancel', 'label': 'Cancel work', 'style': 'danger'}]

    def before_infer(self, run):
        with self.store.lock:
            root = self.root(run)
            if self.halted(run):
                raise LoopStopped(root.get('safety_reason') or 'Workflow stopped by owner.', 409)
            run_count = max(run.get('inference_attempts', 0), run.get('steps', 0) - run.get('budget_step_offset', 0))
            total = root.get('family_attempts', 0)
            if not root.get('budget_unlimited'):
                limits = self.budget()
                try:
                    tokens = max(0, int(root.get('family_tokens') or 0) - int(root.get('family_tokens_offset') or 0))
                except (TypeError, ValueError):
                    tokens = 0
                tail = ' Your decision is required before another model request.'
                if run_count >= limits['run_turns']:
                    name = self.store.get('agents', run['agent_id']).get('name', run['agent_id'])
                    self.halt(run, f"{name} used {run_count} model turns on one task (limit {limits['run_turns']})." + tail, 'budget', 'run_turns')
                if total >= limits['project_turns']:
                    self.halt(run, f"This project used {total} model turns across all agents (limit {limits['project_turns']})." + tail, 'budget', 'project_turns')
                if limits['project_tokens'] and tokens >= limits['project_tokens']:
                    self.halt(run, f"This project used {tokens:,} tokens (limit {limits['project_tokens']:,})." + tail, 'budget', 'project_tokens')
            self.store.patch('runs', run['id'], {'inference_attempts': run_count + 1})
            self.store.patch('runs', root['id'], {'family_attempts': total + 1})

    def refund(self, run):
        """A turn that never produced a model answer (usage limit) must not eat the turn budget."""
        with self.store.lock:
            current = self.store.get('runs', run['id'])
            self.store.patch('runs', current['id'], {'inference_attempts': max(0, current.get('inference_attempts', 0) - 1)})
            root = self.root(current)
            self.store.patch('runs', root['id'], {'family_attempts': max(0, root.get('family_attempts', 0) - 1)})

    def before_child(self, root_id, kind):
        if kind in ('access_approval','browser_approval','command_approval','safety_approval'):
            return
        root = self.store.get('runs', root_id)
        if self.halted(root):
            raise LoopStopped('Workflow paused; only the owner can resume it.', 409)
        if root.get('budget_unlimited'):
            return
        limit = self.budget()['project_runs']
        family = [r for r in self.store.all('runs') if r.get('root_id') == root_id and not r.get('system_managed')]
        if len(family)-root.get('family_runs_offset',0) >= limit:
            self.halt(root, f'This work created {len(family)-root.get("family_runs_offset",0)} agent runs from messages or subagents (limit {limit}) and stopped before another model request.', 'budget', 'project_runs')

    def tool_result(self, run, call, result):
        if isinstance(result, dict) and result.get('waiting_permission'):
            return
        # Ignore prose: compare the actual requested operation and observed result.
        signature = hashlib.sha256(json.dumps([call, result], sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
        current = self.store.get('runs', run['id'])
        recent = (current.get('operation_fingerprints', []) + [signature])[-12:]
        self.store.patch('runs', run['id'], {'operation_fingerprints': recent})
        consecutive=len(recent)>=3 and len(set(recent[-3:]))==1
        cycle=len(recent)>=6 and recent[-6:-4]==recent[-4:-2]==recent[-2:]
        failed=isinstance(result,dict) and bool(result.get('error')) and recent.count(signature)>=3
        if consecutive or cycle or failed:
            self.halt(run, 'The agent called the same tool with the same result three times without progress: ' + call['name'])

    def answer(self, question, answer):
        value = answer.get('safety_action')
        offered = {a['value'] for a in question.get('actions') or []} or {'continue', 'cancel'}
        if value not in ('continue','allow_task','cancel') or value not in offered:
            raise DomainError('Choose one of the decision buttons.', 400)
        root = self.store.get('runs', question['safety_root'])
        if value == 'cancel':
            self.engine.cancel(root['id'], cascade=True)
            return
        changes = {'safety_halted': False, 'family_attempts': 0, 'safety_reason': '', 'safety_kind': None, 'safety_budget': None,
            'family_tokens_offset': root.get('family_tokens', 0) or 0,
            'family_runs_offset':sum(r.get('root_id')==root['id'] and not r.get('system_managed') for r in self.store.all('runs'))}
        if value == 'allow_task':
            changes['budget_unlimited'] = True
        self.store.patch('runs', root['id'], changes)
        for run in self.store.all('runs'):
            if run.get('root_id') == root['id'] and run.get('safety_previous_status') and run['status'] not in ('cancelled','done','failed'):
                previous = run['safety_previous_status']
                self.store.patch('runs', run['id'], {'status': 'queued' if previous == 'running' else previous,
                    'safety_previous_status': None, 'inference_attempts': 0, 'operation_fingerprints': [],
                    'budget_step_offset': run.get('steps', 0), 'text_only_turns':0})

    def stop_all(self):
        self.store.settings({'execution_enabled': False, 'emergency_stopped_at': now()})
        self.engine.commands.sessions.clear()
        for run in self.store.all('runs'):
            if run['status'] not in ('done','failed','cancelled'):
                self.engine.cancel(run['id'], cascade=True)
        for task in self.store.all('tasks'):
            if task['status'] not in ('done','failed','cancelled','rejected'):
                self.store.patch('tasks', task['id'], {'status': 'cancelled'})
        for item in self.store.all('channel_outbox'):
            if item['status']=='queued':self.store.patch('channel_outbox',item['id'],{'status':'cancelled'})
        self.store.event('All agents stopped', 'Execution disabled. Queued and active work cancelled. Resume only when the owner enables execution again.', 'system')
        return {'stopped': True}
