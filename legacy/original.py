"""The code exactly as supplied in the ORBIT brief, kept for before/after comparison.

Run with ``python legacy/original.py``. Nothing in :mod:`orbit_scheduler` imports this
module -- it exists only so the refactor can be diffed against its starting point.

Known defects, each addressed in the refactor (see README, "What the legacy code got wrong"):

1. ``executed`` never resets, so a *daily* quota silently becomes a lifetime quota.
2. Exact ``'%H:%M'`` matching double-executes when two ticks land in one minute, and
   drops the task entirely when no tick lands inside the scheduled minute.
3. ``users[user]`` raises ``KeyError`` for an unknown user, killing the whole loop.
4. Quota is consumed before the work is known to have succeeded.
"""

import datetime

users = {
    'alice': {'quota': 3, 'executed': 0},
    'bob': {'quota': 5, 'executed': 0},
}

tasks = [
    {'user': 'alice', 'time': '12:00', 'action': 'sync', 'target': '/data/x'},
    {'user': 'bob', 'time': '12:00', 'action': 'backup', 'target': '/srv/y'},
    {'user': 'alice', 'time': '12:00', 'action': 'delete', 'target': '/tmp/z'},
]


def run():
    now = datetime.datetime.now().strftime('%H:%M')
    for task in tasks:
        if task['time'] == now:
            user = task['user']
            if users[user]['executed'] >= users[user]['quota']:
                print(f"{user} has exceeded quota.")
                continue
            print(f"Executing {task['action']} on {task['target']} for {user}")
            users[user]['executed'] += 1


if __name__ == '__main__':
    run()
