import os

replacements = {
    'from app.adapters.onebot_v11.network.client': 'from app.adapters.onebot_v11.network.client',
    'import app.adapters.onebot_v11.network.client': 'import app.adapters.onebot_v11.network.client',
    
    'from app.adapters.onebot_v11.network.reverse_ws': 'from app.adapters.onebot_v11.network.reverse_ws',
    'import app.adapters.onebot_v11.network.reverse_ws': 'import app.adapters.onebot_v11.network.reverse_ws',
    
    'from app.adapters.onebot_v11.network.senders': 'from app.adapters.onebot_v11.network.senders',
    'import app.adapters.onebot_v11.network.senders': 'import app.adapters.onebot_v11.network.senders',
    
    'from app.adapters.onebot_v11.store.role_store': 'from app.adapters.onebot_v11.store.role_store',
    'import app.adapters.onebot_v11.store.role_store': 'import app.adapters.onebot_v11.store.role_store',
    
    'from app.adapters.onebot_v11.store.action_tracker': 'from app.adapters.onebot_v11.store.action_tracker',
    'import app.adapters.onebot_v11.store.action_tracker': 'import app.adapters.onebot_v11.store.action_tracker',
    
    'from app.adapters.onebot_v11.api.endpoints': 'from app.adapters.onebot_v11.api.endpoints.endpoints',
    'import app.adapters.onebot_v11.api.endpoints': 'import app.adapters.onebot_v11.api.endpoints.endpoints',
}

def process_file(filepath):
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    
    new_content = content
    for old, new in replacements.items():
        new_content = new_content.replace(old, new)
        
    if new_content != content:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(new_content)
        print(f"Updated imports in {filepath}")

for root, _, files in os.walk('.'):
    for f in files:
        if f.endswith('.py'):
            process_file(os.path.join(root, f))
