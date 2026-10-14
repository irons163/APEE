"""Durable macOS result notifications; no external recipient or network channel."""

import hashlib
import json
import plistlib
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from .config import ApeeError, BusyError
from .portfolio import text
from .store import now


def later(seconds):
    return (datetime.fromisoformat(now())+timedelta(seconds=seconds)).isoformat(timespec='seconds')


class Notifications:
    def __init__(self, engine):
        self.engine, self.store, self.config = engine, engine.store, engine.config

    def records(self):
        return [json.loads(row[0]) for row in self.store.db.execute('SELECT data FROM notification_outbox ORDER BY id')]

    def _save(self, item):
        self.store.db.execute('INSERT OR REPLACE INTO notification_outbox VALUES (?, ?)',
                             (item['id'], json.dumps(item, ensure_ascii=False)))

    def mark(self, topic, fingerprint, title=None, body=None, baseline=False):
        key = 'notification_marker:'+topic
        previous = self.store.control(key) or {}
        if previous.get('fingerprint')==fingerprint: return
        sequence = previous.get('sequence', 0)+1
        self.store._set_control(key, dict(fingerprint=fingerprint, sequence=sequence))
        if title is None or baseline: return
        identifier = hashlib.sha256((str(self.config.state_dir)+topic+str(sequence)+fingerprint).encode()).hexdigest()
        self._save(dict(id=identifier, topic=topic, fingerprint=fingerprint, sequence=sequence,
            title=title[:200], body=(body or '')[:600], created=now(), status='pending', attempts=0))

    def collect(self):
        try:
            with self.store.lock('notifications'):
                return self._collect()
        except BusyError:
            return

    def _collect(self):
        if not self.config.notifications['enabled']: return
        cursor = self.store.control('notification_cursor')
        baseline = cursor is None
        with self.store.db:
            rows = self.store.db.execute('SELECT * FROM events WHERE id>? ORDER BY id', (cursor or 0,)).fetchall()
            for event in rows:
                task = self.store.task(event['task_id'])
                run = self.store.run(task['id']) if task['run_id'] else {}
                state = event['state']
                detail = json.loads(event['detail'])
                fingerprint = json.dumps(dict(state=state, run=run.get('id'), error=detail.get('error')), sort_keys=True)
                title, body = None, None
                if state=='ready': title, body = 'APEE · 本機成果待審', task['spec']['title']
                elif state in ('failed', 'interrupted') or (state=='publishing' and detail.get('error')):
                    title, body = 'APEE · 任務需要處理', task['spec']['title']+'：'+str(detail.get('error') or run.get('error') or state)
                self.mark('task:'+task['id'], fingerprint, title, body, baseline)
                if run.get('pr_url') and state in ('review', 'merged', 'closed'):
                    status = run.get('pr_status', {})
                    pr_fingerprint = json.dumps(dict(head=status.get('head', run.get('active_delivery', {}).get('commit', run.get('commit'))),
                        state=status.get('state', 'OPEN'), ci=status.get('ci', 'none'), review=status.get('review', ''),
                        attention=status.get('attention', []), error=run.get('pr_refresh_error', {}).get('message')), sort_keys=True)
                    alert = state in ('merged', 'closed') or bool(status.get('attention')) or bool(run.get('pr_refresh_error'))
                    delivery = state=='review' and (detail.get('url') or detail.get('event') in ('ci_repaired', 'ci_repair_published'))
                    passed = status.get('ci')=='passed'
                    self.mark('pr:'+run['pr_url'], pr_fingerprint,
                        'APEE · PR 成果更新' if alert or delivery or passed else None,
                        task['repo']+' · '+task['spec']['title']+'\n'+(detail.get('summary') or status.get('state', '待審查'))+'\n'+run['pr_url'], baseline)
            if rows: self.store._set_control('notification_cursor', rows[-1]['id'])
            elif baseline: self.store._set_control('notification_cursor', 0)
            for item in self.store.documents('candidates'):
                self.mark('candidate:'+item['id'], item['state'], 'APEE · 改進候選待決定' if item['state']=='pending' else None,
                    item['title']+'\n'+item['reason'], baseline)
            for item in self.store.documents('outcomes'):
                self.mark('outcome:'+item['id'], item['status'], 'APEE · 指標成效更新' if item['status'] in ('confirmed', 'regressed', 'not_met') else None,
                    item['status']+' · '+item['reason'], baseline)
            blocker = self.engine.dispatch_blocker()
            self.mark('dispatch', json.dumps(blocker or {}, sort_keys=True), 'APEE · 新工作暫停' if blocker else None,
                blocker['message'] if blocker else None, baseline)

    def pending(self):
        if not self.config.notifications['enabled']: return []
        ready = []
        with self.store.db:
            for item in self.records():
                marker = self.store.control('notification_marker:'+item['topic']) or {}
                if item['status'] in ('delivered', 'superseded', 'blocked'): continue
                if marker.get('sequence')!=item['sequence']:
                    item['status'] = 'superseded'
                    self._save(item)
                    continue
                if item.get('next_retry', '')>now() or (item['status']=='sending' and item.get('leased_until', '')>now()): continue
                if item['attempts']>=self.config.notifications['max_attempts']:
                    item['status'] = 'blocked'
                    self._save(item)
                    continue
                ready.append(item)
        return sorted(ready, key=lambda x:(x['created'], x['id']))[:10]

    def claim(self, identifier):
        candidate = next((item for item in self.pending() if item['id']==identifier), None)
        if not candidate: return dict(claimed=False)
        previous = json.dumps(candidate, ensure_ascii=False)
        candidate.update(status='sending', token=uuid.uuid4().hex, leased_until=later(90), attempts=candidate['attempts']+1)
        with self.store.db:
            changed = self.store.db.execute('UPDATE notification_outbox SET data=? WHERE id=? AND data=?',
                (json.dumps(candidate, ensure_ascii=False), identifier, previous)).rowcount
        return dict(candidate, claimed=True) if changed else dict(claimed=False)

    def receipt(self, identifier, token, error=None):
        with self.store.db:
            item = next((item for item in self.records() if item['id']==identifier), None)
            if not item or item.get('token')!=token or item['status']!='sending': return dict(accepted=False)
            previous = json.dumps(item, ensure_ascii=False)
            if error:
                item.update(status='failed', error=text(error, '通知錯誤', 2000), next_retry=later(min(3600, 60*2**min(item['attempts'], 5))))
            else:
                item.update(status='delivered', delivered_at=now())
                item.pop('error', None)
            item.pop('token', None)
            item.pop('leased_until', None)
            changed = self.store.db.execute('UPDATE notification_outbox SET data=? WHERE id=? AND data=?',
                (json.dumps(item, ensure_ascii=False), identifier, previous)).rowcount
        return dict(accepted=bool(changed), status=item['status'])

    def retry(self, identifier):
        with self.store.db:
            item = next((item for item in self.records() if item['id']==identifier), None)
            if not item or item['status'] not in ('failed', 'blocked'):
                raise ApeeError('只有失敗通知可以重新嘗試。')
            item.setdefault('retry_history', []).append(dict(at=now(), attempts=item['attempts'], error=item.get('error'), source='user'))
            item.update(status='pending', attempts=0, next_retry=now())
            self._save(item)
        return item

    def snapshot(self):
        records = self.records()
        return dict(policy=self.config.notifications, pending=sum(x['status'] in ('pending', 'sending') for x in records),
            failed=sum(x['status'] in ('failed', 'blocked') for x in records),
            recent=sorted(records, key=lambda x:(x['created'], x['id']), reverse=True)[:30],
            adapter_error=self.store.control('notification_adapter_error'))


def deliver(engine):
    notifier = Notifications(engine)
    notifier.collect()
    if not notifier.pending(): return
    try:
        app = Path(engine.config.notifications['app_path'] or '')
        if not app.is_absolute() or not app.is_dir(): raise ApeeError('尚未設定可用的通知 app；請在桌面啟用通知。')
        backend = json.loads((app/'Contents/Resources/backend.json').read_text())
        info = plistlib.loads((app/'Contents/Info.plist').read_bytes())
        if (info.get('CFBundleIdentifier')!='com.apee.desktop' or Path(backend['config']).resolve()!=engine.config.path
                or Path(backend['root']).resolve()!=Path(__file__).resolve().parents[1]):
            raise ApeeError('通知 app 不屬於目前設定與程式版本。')
        engine.runner.run([str(app/'Contents/MacOS/APEE'), '--deliver-notifications'], timeout=60)
        engine.store.set_control('notification_adapter_error', None)
    except (ApeeError, OSError, ValueError, KeyError) as exc:
        engine.store.set_control('notification_adapter_error', dict(at=now(), message=str(exc)))
