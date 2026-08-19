import sqlite3
import os

db_path = os.path.join(os.path.dirname(__file__), '../app/database/myapi.db')
conn = sqlite3.connect(db_path)
cursor = conn.cursor()

updates = [
    ('smart', 'nvidia/nemotron-3-super-120b-a12b:free'),
    ('coding', 'cohere/north-mini-code:free'),
    ('fast', 'google/gemma-4-26b-a4b-it:free'),
    ('reasoning', 'nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free'),
    ('vision', 'nvidia/nemotron-nano-12b-v2-vl:free')
]

for alias, real_model in updates:
    cursor.execute(
        "UPDATE server_models SET backend_model_name = ?, client_model_name = ? WHERE frontend_model_name = ?",
        (real_model, real_model, alias)
    )

conn.commit()
print('Database model aliases updated successfully! Now restart docker to apply changes...')
