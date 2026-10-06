# Central de Chamados TI - V6

Versão preparada para hospedagem web com Flask + PostgreSQL.

Build: `pip install -r requirements.txt`
Start: `gunicorn app:app`

Sem DATABASE_URL, usa SQLite localmente. Com DATABASE_URL, usa PostgreSQL.
