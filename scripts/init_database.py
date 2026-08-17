#!/usr/bin/env python3
"""
Database initialization script
Creates all the necessary database table structures and optionally
seeds an OpenRouter upstream server when OPENROUTER_API_KEY is set.
"""

import asyncio
import sys
import os

# Add the project root directory to the Python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database.database import init_db


async def seed_openrouter_server():
    """Seed the OpenRouter upstream server if OPENROUTER_API_KEY is configured.

    - If the server does not exist, creates it with default model aliases.
    - If the server already exists, adds only missing aliases (preserves
      any admin-configured models).
    - Updates the API key if it changed in the environment.

    The default model mappings are convenience defaults that can be
    reconfigured from the admin dashboard at any time.
    """
    from app.config.settings import settings

    if not settings.OPENROUTER_API_KEY:
        print("ℹ️  OPENROUTER_API_KEY not set, skipping OpenRouter server seed.")
        return

    from app.database.database import AsyncSessionLocal
    from app.database.models import LLMServer, ServerModel
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    # Default model aliases: frontend_name -> backend_name (OpenRouter model ID)
    default_models = [
        ("free", "openrouter/free"),
        ("smart", "openai/gpt-oss-120b:free"),
        ("coding", "qwen/qwen3-coder-480b-a35b:free"),
        ("fast", "google/gemma-4-26b-a4b:free"),
        ("reasoning", "nvidia/nemotron-3-ultra:free"),
        ("vision", "nvidia/nemotron-nano-2-vl:free"),
        ("small", "openai/gpt-oss-20b:free"),
    ]

    async with AsyncSessionLocal() as session:
        # Check if the OpenRouter server already exists (eagerly load models)
        result = await session.execute(
            select(LLMServer)
            .options(selectinload(LLMServer.models))
            .where(LLMServer.server_url == settings.OPENROUTER_BASE_URL)
        )
        existing = result.scalar_one_or_none()

        if existing:
            # Update the API key if it changed
            if existing.apikey != settings.OPENROUTER_API_KEY:
                existing.apikey = settings.OPENROUTER_API_KEY
                print("✅ OpenRouter API key updated from environment.")

            # Merge missing aliases — do NOT overwrite admin-configured models
            existing_frontend_names = set()
            for m in existing.models:
                name = m.frontend_model_name or m.actual_model_name
                existing_frontend_names.add(name)

            added = []
            for frontend_name, backend_name in default_models:
                if frontend_name not in existing_frontend_names:
                    model = ServerModel(
                        client_model_name=backend_name,
                        actual_model_name=frontend_name,
                        backend_model_name=backend_name,
                        frontend_model_name=frontend_name,
                        reqs=0,
                        status=True,
                        input_token_weight=1.0,
                        output_token_weight=1.0,
                    )
                    existing.models.append(model)
                    added.append(f"{frontend_name} → {backend_name}")

            await session.commit()

            if added:
                print(f"✅ Added {len(added)} missing model alias(es) to OpenRouter server:")
                for a in added:
                    print(f"   {a}")
            else:
                print(f"ℹ️  OpenRouter server at {settings.OPENROUTER_BASE_URL} already has all default aliases.")
            return

        # Server does not exist — create it with all default aliases
        server = LLMServer(
            server_url=settings.OPENROUTER_BASE_URL,
            device="openrouter",
            apikey=settings.OPENROUTER_API_KEY,
        )

        for frontend_name, backend_name in default_models:
            model = ServerModel(
                client_model_name=backend_name,
                actual_model_name=frontend_name,
                backend_model_name=backend_name,
                frontend_model_name=frontend_name,
                reqs=0,
                status=True,
                input_token_weight=1.0,
                output_token_weight=1.0,
            )
            server.models.append(model)

        session.add(server)
        await session.commit()
        print(f"✅ OpenRouter server seeded at {settings.OPENROUTER_BASE_URL}")
        print(f"   Default aliases: {', '.join(fn for fn, _ in default_models)}")
        print("   You can reconfigure models from the admin dashboard.")


async def main():
    """Main function"""
    print("Initializing database table structures...")

    try:
        await init_db()
        print("Database table structures created successfully!")

        # Seed the OpenRouter server if configured
        await seed_openrouter_server()

    except Exception as e:
        print(f"Error during database initialization: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())