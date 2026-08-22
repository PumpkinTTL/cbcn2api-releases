import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from src.storage import store
print('DB_DIR:', store.DB_DIR, flush=True)
print('DB_PATH:', store.DB_PATH, flush=True)
accounts = store.list_accounts("workbuddy")
print(f'Total accounts: {len(accounts)}', flush=True)
for a in accounts:
    print(f'  platform=workbuddy, email={a.email}, uid={a.uid}, domain={a.domain}', flush=True)
