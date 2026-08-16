#!/usr/bin/env python3
"""
Database initialization script
Creates all the necessary database table structures
"""

import asyncio
import sys
import os

# Add the project root directory to the Python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database.database import init_db


async def main():
    """Main function"""
    print("Initializing database table structures...")

    try:
        await init_db()
        print("Database table structures created successfully!")
    except Exception as e:
        print(f"Error during database initialization: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())