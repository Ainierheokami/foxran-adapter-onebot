import os
import shutil

# Make dirs safely
for d in ['network', 'store', 'api']:
    os.makedirs(d, exist_ok=True)
    init_file = os.path.join(d, '__init__.py')
    if not os.path.exists(init_file):
        open(init_file, 'w').close()

# Move files safely
moves = {
    'client.py': 'network/client.py',
    'reverse_ws.py': 'network/reverse_ws.py',
    'senders.py': 'network/senders.py',
    'role_store.py': 'store/role_store.py',
    'action_tracker.py': 'store/action_tracker.py',
    'api.py': 'api/endpoints.py'
}

for src, dst in moves.items():
    if os.path.exists(src) and not os.path.exists(dst):
        shutil.move(src, dst)
        print(f'Moved {src} -> {dst}')
