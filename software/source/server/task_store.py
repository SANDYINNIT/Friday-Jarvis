"""Task list for Sir's work: add, list, complete, delete. SQLite, local only.

FRIDAY had reminders (time-based) and a calendar, but no actual TO-DO list, so
"what do I have to do?" had no honest answer. This is the missing piece from the
project brief.

Like every helper in this project it is a CAPABILITY THE BRAIN CHOOSES - never a
phrase-matched trigger. The brain is told about it in the live system prompt
(profiles/default.py) and decides when a request is really about tasks.

Storage follows the reminders module (reminders.py): a SQLite file in
~/.friday, created on demand, so nothing is written at import time.

    from source.server.task_store import TaskStore
    store = TaskStore()
    store.add("send the supplier the new invoice", due="2026-09-30 17:00")
    store.list_tasks()          # open tasks, newest last
    store.complete(3)

Import form note: the brain's kernel CWD is the project root, so the working
import is `from source.server.task_store import TaskStore`. A bare
`from task_store import ...` does not resolve.
"""

from __future__ import annotations

import os
from contextlib import closing, contextmanager
import hashlib
import json
import re
import sqlite3
import time
from datetime import datetime, timedelta

DEFAULT_DB_PATH = os.path.join(os.path.expanduser("~"), ".friday", "friday_tasks.db")
MAX_TITLE_CHARS = 300
MAX_NOTES_CHARS = 2_000
MAX_TASKS_RETURNED = 200

PRIORITIES = ("low", "normal", "high")


def default_db_path() -> str:
    return os.environ.get("FRIDAY_TASK_DB", DEFAULT_DB_PATH)


def normalize_priority(value) -> str:
    text = str(value or "").strip().lower()
    if text in ("high", "urgent", "important"):
        return "high"
    if text in ("low", "someday", "minor"):
        return "low"
    return "normal"


def parse_due(text, now=None):
    """Understand the due dates a professional actually types.

    Accepts: "today", "tomorrow", "tonight", "friday"/"mon", "in 2 hours",
    "in 30 minutes", "next week", "2026-09-30 17:00", "5pm", "at 17:00".
    Returns (iso_string_or_None, human_label, ok).
    """
    raw = str(text or "").strip()
    if not raw:
        return None, "", True
    now = now or datetime.now()
    lowered = raw.lower()

    def at(hour, minute=0, days=0):
        """Resolve a clock time, rolling forward a day if it has already passed."""
        target = (now + timedelta(days=days)).replace(hour=hour, minute=minute,
                                                      second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target

    def describe(target, spoken):
        """Label derived from the RESOLVED target, so it can never disagree.

        Returns the friendly word only when the resolved date actually matches
        it; otherwise a real calendar date, which is always honest.
        """
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        offset = (target.date() - midnight.date()).days
        clock = target.strftime("%H:%M")
        if offset == 0:
            return f"{spoken} {clock}"
        if offset == 1:
            return f"tomorrow {clock}"
        return target.strftime("%a %d %b") + f" {clock}"

    if lowered == "today":
        return at(18, 0).strftime("%Y-%m-%d %H:%M"), describe(at(18, 0), "today"), True
    if lowered == "tonight":
        return at(21, 0).strftime("%Y-%m-%d %H:%M"), describe(at(21, 0), "tonight"), True
    if lowered == "this evening":
        return at(18, 0).strftime("%Y-%m-%d %H:%M"), describe(at(18, 0), "this evening"), True
    if lowered == "tomorrow":
        return at(9, 0, 1).strftime("%Y-%m-%d %H:%M"), describe(at(9, 0, 1), "tomorrow"), True
    if lowered in ("next week", "nextweek"):
        return at(9, 0, 7).strftime("%Y-%m-%d %H:%M"), describe(at(9, 0, 7), "next week"), True
    if lowered == "asap":
        return at(12, 0).strftime("%Y-%m-%d %H:%M"), describe(at(12, 0), "asap"), True

    match = re.match(r"^in\s+(\d+)\s*(minute|min|hour|hr|day|week)s?$", lowered)
    if match:
        amount = int(match.group(1))
        unit = match.group(2)
        delta = {"minute": timedelta(minutes=amount), "min": timedelta(minutes=amount),
                 "hour": timedelta(hours=amount), "hr": timedelta(hours=amount),
                 "day": timedelta(days=amount), "week": timedelta(weeks=amount)}[unit]
        target = now + delta
        return target.strftime("%Y-%m-%d %H:%M"), f"in {amount} {unit}(s)", True

    days = {"monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2,
            "wed": 2, "thursday": 3, "thu": 3, "thurs": 3, "friday": 4, "fri": 4,
            "saturday": 5, "sat": 5, "sunday": 6, "sun": 6}
    for name, index in sorted(days.items(), key=lambda kv: -len(kv[0])):
        if lowered.startswith(name):
            ahead = (index - now.weekday()) % 7
            target = (now + timedelta(days=ahead)).replace(hour=9, minute=0, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=7)
            spoken = "today" if ahead == 0 else f"next {name}"
            return target.strftime("%Y-%m-%d %H:%M"), describe(target, spoken), True

    clock = re.match(r"^(?:at\s*)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)$", lowered)
    if clock:
        hour = int(clock.group(1)) % 12
        if clock.group(3) == "pm":
            hour += 12
        # at() rolls an already-passed time to tomorrow; describe() keeps the
        # label in step with the date actually stored.
        target = at(hour, int(clock.group(2) or 0))
        return target.strftime("%Y-%m-%d %H:%M"), describe(target, "today"), True

    try:
        parsed = datetime.strptime(raw, "%Y-%m-%d %H:%M")
        return parsed.strftime("%Y-%m-%d %H:%M"), raw, True
    except ValueError:
        pass
    try:
        parsed = datetime.strptime(raw, "%Y-%m-%d")
        return parsed.strftime("%Y-%m-%d 09:00"), raw, True
    except ValueError:
        pass
    return None, raw, False


class TaskStore:
    """SQLite task list. Public methods return dicts and never raise."""

    def __init__(self, db_path=None):
        self.db_path = db_path or default_db_path()
        self._ready = False

    # -- plumbing ----------------------------------------------------------- #
    @contextmanager
    def _connect(self):
        folder = os.path.dirname(self.db_path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with closing(sqlite3.connect(self.db_path, timeout=10)) as conn, conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            yield conn

    def _ensure(self) -> bool:
        if self._ready:
            return True
        try:
            with self._connect() as conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS tasks ("
                    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    " title TEXT NOT NULL,"
                    " notes TEXT DEFAULT '',"
                    " priority TEXT DEFAULT 'normal',"
                    " status TEXT DEFAULT 'open',"
                    " due_at TEXT DEFAULT '',"
                    " due_label TEXT DEFAULT '',"
                    " created_at REAL NOT NULL,"
                    " completed_at REAL)"
                )
            self._ready = True
            return True
        except Exception as error:
            print(f"[task_store] database unavailable: {error}", flush=True)
            return False

    @staticmethod
    def _row(row):
        if row is None:
            return None
        overdue = False
        if row["due_at"] and row["status"] == "open":
            try:
                overdue = datetime.strptime(row["due_at"], "%Y-%m-%d %H:%M") < datetime.now()
            except ValueError:
                overdue = False
        return {
            "id": row["id"],
            "title": row["title"],
            "notes": row["notes"],
            "priority": row["priority"],
            "status": row["status"],
            "due_at": row["due_at"],
            "due_label": row["due_label"],
            "overdue": overdue,
            "created_at": row["created_at"],
            "revision": hashlib.sha256(json.dumps(dict(row),sort_keys=True).encode()).hexdigest(),
        }

    # -- api ---------------------------------------------------------------- #
    @staticmethod
    def _retire_receipts(conn, ident):
        """Tombstone replay results in the SAME transaction as deliberate deletion.

        Keep operation hashes so retries cannot repeat the action, but do not
        retain deleted task text or claim that its old saved result still exists.
        Python JSON parsing avoids requiring an optional SQLite JSON extension.
        """
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='personal_task_receipts'").fetchone():
            return
        updates=[]
        tombstone=json.dumps({'ok':False,'deleted':'task-'+str(ident),
            'message':'This task was deleted after the recorded operation. Nothing was repeated.'})
        for row in conn.execute('SELECT operation,result FROM personal_task_receipts'):
            result=json.loads(row['result'])
            task=result.get('task') if isinstance(result,dict) else None
            if isinstance(task,dict) and type(task.get('id')) is int and task['id']==ident:
                updates.append((tombstone,row['operation']))
        conn.executemany('UPDATE personal_task_receipts SET result=? WHERE operation=?',updates)

    def mutate(self, reference, action, values, expected=None, operation_id=None):
        """Explicit local UI mutation; exact personal domain and optimistic row revision."""
        if action not in ('create','update','complete','reopen','delete') or not isinstance(values,dict):
            return {'ok':False,'message':'Unsupported personal task action.'}
        if action in ('complete','reopen','delete') and values:
            return {'ok':False,'message':'This state action accepts no editable fields.'}
        if operation_id is not None and (not isinstance(operation_id,str) or not re.fullmatch(r'[a-f0-9]{32}',operation_id)):
            return {'ok':False,'message':'Invalid task operation identifier.'}
        if set(values)-{'title','notes','priority','due'}:
            return {'ok':False,'message':'Unknown personal task field.'}
        if action=='create':
            if reference is not None:return {'ok':False,'message':'New personal tasks have no existing reference.'}
        elif not isinstance(reference,str) or not re.fullmatch(r'task-[1-9][0-9]*',reference):
            return {'ok':False,'message':'A personal task reference is required.'}
        for key,limit in (('title',MAX_TITLE_CHARS),('notes',MAX_NOTES_CHARS),('due',120)):
            if key in values and (not isinstance(values[key],str) or len(values[key])>limit):
                return {'ok':False,'message':'A task field exceeds its text limit.'}
        if ('title' in values and not values['title'].strip()) or (action=='create' and not values.get('title','').strip()):
            return {'ok':False,'message':'A task needs a title.'}
        if 'priority' in values and values['priority'] not in PRIORITIES:
            return {'ok':False,'message':'Unknown task priority.'}
        if not self._ensure():return {'ok':False,'message':'Personal task storage unavailable.'}
        try:
            with self._connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                fingerprint=hashlib.sha256(json.dumps([reference,action,values],sort_keys=True).encode()).hexdigest()
                if operation_id is not None:
                    conn.execute('CREATE TABLE IF NOT EXISTS personal_task_receipts(operation TEXT PRIMARY KEY,payload_hash TEXT NOT NULL,result TEXT NOT NULL)')
                    receipt=conn.execute('SELECT payload_hash,result FROM personal_task_receipts WHERE operation=?',(operation_id,)).fetchone()
                    if receipt:
                        if receipt[0]!=fingerprint:return {'ok':False,'message':'Operation identifier belongs to another task change.'}
                        cached=json.loads(receipt[1])
                        if cached.get('ok') is True:
                            task=cached.get('task')
                            if not isinstance(task,dict) or type(task.get('id')) is not int:
                                return {'ok':False,'duplicate':True,'message':'The recorded task reference is unavailable.'}
                            if not conn.execute('SELECT 1 FROM tasks WHERE id=?',(task['id'],)).fetchone():
                                # Old versions did not tombstone receipts on
                                # deletion. Refuse replay without scanning or
                                # backfilling historical personal records.
                                return {'ok':False,'duplicate':True,'deleted':'task-'+str(task['id']),
                                    'message':'This task was deleted after the recorded operation. Nothing was repeated.'}
                        return {**cached,'duplicate':True}
                if action=='create':
                    due,label,_=parse_due(values.get('due'))
                    ident=conn.execute('INSERT INTO tasks(title,notes,priority,due_at,due_label,created_at) VALUES(?,?,?,?,?,?)',
                        (values['title'].strip(),values.get('notes',''),values.get('priority','normal'),due or '',label or '',time.time())).lastrowid
                else:
                    ident=int(reference[5:]);raw=conn.execute('SELECT * FROM tasks WHERE id=?',(ident,)).fetchone()
                    if raw is None or self._row(raw)['revision']!=expected:
                        return {'ok':False,'conflict':True,'message':'This personal task changed. Refresh before saving.'}
                    if action=='delete':
                        conn.execute('DELETE FROM tasks WHERE id=?',(ident,))
                        self._retire_receipts(conn,ident)
                        return {'ok':True,'deleted':reference,'message':'Personal task deleted.'}
                    if action in ('complete','reopen'):
                        conn.execute('UPDATE tasks SET status=?,completed_at=? WHERE id=?',
                                     ('done' if action=='complete' else 'open',time.time() if action=='complete' else None,ident))
                    else:
                        updated=dict(raw)
                        for key in ('title','notes','priority'):
                            if key in values:updated[key]=values[key].strip() if key=='title' else values[key]
                        if 'due' in values:updated['due_at'],updated['due_label'],_=parse_due(values['due'])
                        conn.execute('UPDATE tasks SET title=?,notes=?,priority=?,due_at=?,due_label=? WHERE id=?',
                            (updated['title'],updated['notes'],updated['priority'],updated['due_at'] or '',updated['due_label'] or '',ident))
                saved=self._row(conn.execute('SELECT * FROM tasks WHERE id=?',(ident,)).fetchone())
                outcome={'ok':True,'task':saved,'reference':'task-'+str(ident),'message':'Personal task saved.'}
                if operation_id is not None:conn.execute('INSERT INTO personal_task_receipts VALUES(?,?,?)',(operation_id,fingerprint,json.dumps(outcome)))
            return outcome
        except Exception:
            return {'ok':False,'message':'Personal task change was not saved.'}
    def add(self, title, due=None, priority=None, notes=None) -> dict:
        """Create a task. `due` is natural language; unparseable is kept as a label."""
        clean = str(title or "").strip()[:MAX_TITLE_CHARS]
        if not clean:
            return {"ok": False, "error": "a task needs a title"}
        if not self._ensure():
            return {"ok": False, "error": "task database is unavailable"}
        due_at, due_label, _parsed = parse_due(due)
        try:
            with self._connect() as conn:
                cursor = conn.execute(
                    "INSERT INTO tasks (title, notes, priority, due_at, due_label, created_at)"
                    " VALUES (?,?,?,?,?,?)",
                    (clean, str(notes or "")[:MAX_NOTES_CHARS], normalize_priority(priority),
                     due_at or "", due_label or "", time.time()),
                )
                row = conn.execute("SELECT * FROM tasks WHERE id=?", (cursor.lastrowid,)).fetchone()
        except Exception as error:
            return {"ok": False, "error": f"could not save the task: {error}"}
        return {"ok": True, "task": self._row(row)}

    def list_tasks(self, status="open", limit=MAX_TASKS_RETURNED) -> dict:
        """Open tasks (default) ordered by due date, undated last, then newest."""
        if not self._ensure():
            return {"ok": False, "error": "task database is unavailable", "tasks": []}
        sql = "SELECT * FROM tasks"
        params = []
        if status and status != "all":
            sql += " WHERE status=?"
            params.append("done" if status in ("done", "completed") else "open")
        sql += " ORDER BY (due_at = '') ASC, due_at ASC, id ASC LIMIT ?"
        params.append(max(1, min(int(limit), MAX_TASKS_RETURNED)))
        try:
            with self._connect() as conn:
                rows = conn.execute(sql, params).fetchall()
        except Exception as error:
            return {"ok": False, "error": f"could not read tasks: {error}", "tasks": []}
        tasks = [self._row(row) for row in rows]
        return {"ok": True, "status": "all" if status == "all" else status,
                "count": len(tasks), "tasks": tasks}

    def complete(self, task_id) -> dict:
        """Mark a task done by id."""
        return self._set_status(task_id, "done")

    def reopen(self, task_id) -> dict:
        """Undo a completion."""
        return self._set_status(task_id, "open")

    def _set_status(self, task_id, status) -> dict:
        if not self._ensure():
            return {"ok": False, "error": "task database is unavailable"}
        try:
            ident = int(task_id)
        except (TypeError, ValueError):
            return {"ok": False, "error": f"'{task_id}' is not a task id"}
        try:
            with self._connect() as conn:
                cursor = conn.execute(
                    "UPDATE tasks SET status=?, completed_at=? WHERE id=?",
                    (status, time.time() if status == "done" else None, ident),
                )
                if cursor.rowcount == 0:
                    return {"ok": False, "error": f"no task with id {ident}"}
                row = conn.execute("SELECT * FROM tasks WHERE id=?", (ident,)).fetchone()
        except Exception as error:
            return {"ok": False, "error": f"could not update the task: {error}"}
        return {"ok": True, "task": self._row(row)}

    def delete(self, task_id) -> dict:
        """Remove a task outright."""
        if not self._ensure():
            return {"ok": False, "error": "task database is unavailable"}
        try:
            ident = int(task_id)
        except (TypeError, ValueError):
            return {"ok": False, "error": f"'{task_id}' is not a task id"}
        try:
            with self._connect() as conn:
                cursor = conn.execute("DELETE FROM tasks WHERE id=?", (ident,))
                if cursor.rowcount:
                    self._retire_receipts(conn,ident)
        except Exception as error:
            return {"ok": False, "error": f"could not delete the task: {error}"}
        if cursor.rowcount == 0:
            return {"ok": False, "error": f"no task with id {ident}"}
        return {"ok": True, "deleted": ident}

    def summary(self) -> dict:
        """Counts the brain can speak without listing everything."""
        if not self._ensure():
            return {"ok": False, "error": "task database is unavailable"}
        try:
            with self._connect() as conn:
                rows = conn.execute("SELECT status, due_at, priority FROM tasks").fetchall()
        except Exception as error:
            return {"ok": False, "error": f"could not read tasks: {error}"}
        now = datetime.now()
        overdue = 0
        for row in rows:
            if row["status"] != "open" or not row["due_at"]:
                continue
            try:
                if datetime.strptime(row["due_at"], "%Y-%m-%d %H:%M") < now:
                    overdue += 1
            except ValueError:
                continue
        open_tasks = sum(1 for row in rows if row["status"] == "open")
        return {
            "ok": True,
            "open": open_tasks,
            "done": sum(1 for row in rows if row["status"] == "done"),
            "overdue": overdue,
            "high_priority": sum(1 for row in rows if row["status"] == "open" and row["priority"] == "high"),
        }
