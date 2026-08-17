#!/bin/bash
set -e

# Check environment variables
if [ -z "$SESSION_SECRET_KEY" ]; then
    echo "⚠️  WARNING: SESSION_SECRET_KEY not set, generating random key..."
    export SESSION_SECRET_KEY=$(python -c "import secrets; print(secrets.token_hex(32))")
    echo "Generated a random SESSION_SECRET_KEY (valid only for this container lifecycle)"
fi

if [ -z "$ADMIN_PASSWORD_HASH" ]; then
    echo "⚠️  WARNING: ADMIN_PASSWORD_HASH not set, using default password 'admin'"
    echo "⚠️  The default password is 'admin', which is a security risk. Please log in and change the password as soon as possible!"
    # Generate the bcrypt hash of the default password 'admin' at runtime
    export ADMIN_PASSWORD_HASH=$(python -c "import bcrypt; print(bcrypt.hashpw(b'admin', bcrypt.gensalt()).decode())")
    echo "Please change the default password after first login!"
fi

# Initialize the database
echo "📦 Initializing database..."
python scripts/init_database.py

echo "✅ Database initialized successfully!"

# Execute the passed command
exec "$@"