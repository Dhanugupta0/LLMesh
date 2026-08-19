"""Seed LLM server configurations for OVO.

Run: python3 -m scripts.seed_servers
"""
import asyncio
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database.database import AsyncSessionLocal, engine
from app.database.models import Base


async def seed():
    from app.services.api_service import ApiService

    # Create tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with AsyncSessionLocal() as session:
        api_service = ApiService()

        servers_data = {
            # ── OpenRouter (free models) ────────────────────
            "https://openrouter.ai/api/v1": {
                "device": "openrouter",
                "apikey": os.getenv("OPENROUTER_API_KEY", ""),
                "model": {
                    # NVIDIA Nemotron 3 Super 120B — heavy/coding
                    "nvidia/nemotron-3-super-120b-a12b:free": {
                        "name": "nvidia/nemotron-3-super-120b-a12b:free",
                        "status": True,
                        "reqs": 0,
                        "input_token_weight": 1.0,
                        "output_token_weight": 1.0,
                    },
                    # OpenAI GPT-OSS 20B — fast
                    "openai/gpt-oss-20b:free": {
                        "name": "openai/gpt-oss-20b:free",
                        "status": True,
                        "reqs": 0,
                        "input_token_weight": 1.0,
                        "output_token_weight": 1.0,
                    },
                    # Google Gemma 4 31B — general
                    "google/gemma-4-31b-it:free": {
                        "name": "google/gemma-4-31b-it:free",
                        "status": True,
                        "reqs": 0,
                        "input_token_weight": 1.0,
                        "output_token_weight": 1.0,
                    },
                    # Z.ai GLM 5.2 — general (also on OpenRouter free)
                    "z-ai/glm-5.2:free": {
                        "name": "z-ai/glm-5.2:free",
                        "status": True,
                        "reqs": 0,
                        "input_token_weight": 1.0,
                        "output_token_weight": 1.0,
                    },
                    # Cohere North Mini Code — coding
                    "cohere/north-mini-code:free": {
                        "name": "cohere/north-mini-code:free",
                        "status": True,
                        "reqs": 0,
                        "input_token_weight": 1.0,
                        "output_token_weight": 1.0,
                    },
                },
            },
            # ── Groq ────────────────────────────────────────
            "https://api.groq.com/openai/v1": {
                "device": "groq",
                "apikey": os.getenv("GROQ_API_KEY", ""),
                "model": {
                    # GPT-OSS 120B — heavy coding
                    "openai/gpt-oss-120b": {
                        "name": "openai/gpt-oss-120b",
                        "status": True,
                        "reqs": 0,
                        "input_token_weight": 1.0,
                        "output_token_weight": 1.0,
                    },
                    # GPT-OSS 20B — fast coding
                    "openai/gpt-oss-20b": {
                        "name": "openai/gpt-oss-20b",
                        "status": True,
                        "reqs": 0,
                        "input_token_weight": 1.0,
                        "output_token_weight": 1.0,
                    },
                },
            },
            # ── NVIDIA NIM ──────────────────────────────────
            "https://integrate.api.nvidia.com/v1": {
                "device": "nvidia",
                "apikey": os.getenv("NVIDIA_NIM_API_KEY", ""),
                "model": {
                    # GLM 5.2
                    "z-ai/glm-5.2": {
                        "name": "z-ai/glm-5.2",
                        "status": True,
                        "reqs": 0,
                        "input_token_weight": 1.0,
                        "output_token_weight": 1.0,
                    },
                },
            },
        }

        await api_service.save_llm_servers(servers_data, session)
        print("✓ Servers seeded successfully!")

        # Verify
        loaded = await api_service.load_llm_servers(session)
        for url, info in loaded.items():
            models = list(info.get("model", {}).keys())
            print(f"  {info.get('device', '?'):12s} │ {url}")
            for m in models:
                print(f"               │   ● {m}")


if __name__ == "__main__":
    # Load .env
    from pathlib import Path
    env_path = Path(__file__).parent.parent / ".env"
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, val = line.partition("=")
                    os.environ.setdefault(key.strip(), val.strip())

    asyncio.run(seed())
