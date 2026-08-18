"""CLI-specific API endpoints.

These endpoints provide richer metadata than the standard OpenAI-compatible
API, specifically designed for the LLMesh CLI's model discovery, status
reporting, and provider health features.
"""

import json
from typing import List, Dict, Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database.database import get_db_session
from app.database.models import LLMServer, ServerModel
from app.utils.logging_config import get_logger

logger = get_logger(__name__)

cli_router = APIRouter(prefix="/api/cli", tags=["CLI"])


def _guess_provider(server_url: str) -> str:
    """Infer provider name from the server URL."""
    url = server_url.lower()
    if "groq" in url:
        return "Groq"
    elif "nvidia" in url or "nim" in url or "integrate.api.nvidia" in url:
        return "NVIDIA NIM"
    elif "openrouter" in url:
        return "OpenRouter"
    elif "openai" in url:
        return "OpenAI"
    elif "anthropic" in url:
        return "Anthropic"
    elif "together" in url:
        return "Together AI"
    elif "fireworks" in url:
        return "Fireworks"
    elif "cerebras" in url:
        return "Cerebras"
    elif "mistral" in url:
        return "Mistral"
    elif "deepseek" in url:
        return "DeepSeek"
    elif "localhost" in url or "127.0.0.1" in url:
        return "Local"
    else:
        return "Custom"


def _guess_modes(model_name: str) -> List[str]:
    """Infer supported modes from the model name."""
    name = model_name.lower()
    modes = []

    # Coding models
    if any(kw in name for kw in ["code", "coding", "coder", "starcoder", "codellama", "deepseek-coder"]):
        modes.append("coding")

    # Thinking/reasoning models
    if any(kw in name for kw in ["think", "reason", "nemotron", "glm", "o1", "o3"]):
        modes.append("thinking")

    # Fast models
    if any(kw in name for kw in ["flash", "mini", "small", "lite", "fast", "turbo", "instant"]):
        modes.append("fast")

    # Default: all models can be used for coding and plan
    if not modes:
        modes = ["coding", "fast"]

    if "plan" not in modes:
        modes.append("plan")

    return modes


@cli_router.get("/models")
async def list_cli_models(session: AsyncSession = Depends(get_db_session)):
    """List all available models with rich metadata for the CLI."""
    try:
        result = await session.execute(
            select(ServerModel, LLMServer)
            .join(LLMServer, ServerModel.server_id == LLMServer.id)
        )
        rows = result.all()

        models = []
        for server_model, llm_server in rows:
            frontend_name = server_model.frontend_model_name or server_model.actual_model_name
            backend_name = server_model.backend_model_name or server_model.client_model_name
            provider = _guess_provider(llm_server.server_url)

            # Parse JSON fields if they exist (new extended columns)
            capabilities = []
            modes = []
            context_window = 0
            tool_support = False
            vision_support = False
            reasoning_support = False
            streaming_support = True
            priority = 0
            weight = 1.0

            # Try extended columns (gracefully degrade if not present)
            if hasattr(server_model, 'capabilities') and server_model.capabilities:
                try:
                    capabilities = json.loads(server_model.capabilities)
                except (json.JSONDecodeError, TypeError):
                    capabilities = []

            if hasattr(server_model, 'modes') and server_model.modes:
                try:
                    modes = json.loads(server_model.modes)
                except (json.JSONDecodeError, TypeError):
                    modes = []

            if hasattr(server_model, 'context_window') and server_model.context_window:
                context_window = server_model.context_window

            if hasattr(server_model, 'tool_support'):
                tool_support = bool(getattr(server_model, 'tool_support', False))
            if hasattr(server_model, 'vision_support'):
                vision_support = bool(getattr(server_model, 'vision_support', False))
            if hasattr(server_model, 'reasoning_support'):
                reasoning_support = bool(getattr(server_model, 'reasoning_support', False))
            if hasattr(server_model, 'streaming_support'):
                streaming_support = bool(getattr(server_model, 'streaming_support', True))
            if hasattr(server_model, 'priority') and server_model.priority is not None:
                priority = server_model.priority
            if hasattr(server_model, 'weight') and server_model.weight is not None:
                weight = server_model.weight
            if hasattr(server_model, 'provider_name') and server_model.provider_name:
                provider = server_model.provider_name

            # Auto-infer modes if not set
            if not modes:
                modes = _guess_modes(frontend_name)

            models.append({
                "name": frontend_name,
                "backend_name": backend_name,
                "provider": provider,
                "server_url": llm_server.server_url,
                "modes": modes,
                "capabilities": capabilities,
                "context_window": context_window,
                "tool_support": tool_support,
                "vision_support": vision_support,
                "reasoning_support": reasoning_support,
                "streaming_support": streaming_support,
                "status": server_model.status if server_model.status is not None else True,
                "priority": priority,
                "weight": weight,
                "input_token_weight": server_model.input_token_weight or 1.0,
                "output_token_weight": server_model.output_token_weight or 1.0,
            })

        # Sort by priority (higher first), then by name
        models.sort(key=lambda m: (-m["priority"], m["name"]))

        return {"models": models, "count": len(models)}

    except Exception as e:
        logger.error(f"CLI models endpoint error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@cli_router.get("/status")
async def get_cli_status(session: AsyncSession = Depends(get_db_session)):
    """Get server status for the CLI."""
    try:
        # Count models and servers
        models_result = await session.execute(select(ServerModel))
        models = models_result.scalars().all()

        servers_result = await session.execute(select(LLMServer))
        servers = servers_result.scalars().all()

        return {
            "online": True,
            "models_count": len(models),
            "providers_count": len(servers),
            "providers": [
                {
                    "name": _guess_provider(s.server_url),
                    "server_url": s.server_url,
                    "models": len([m for m in models if m.server_id == s.id]),
                }
                for s in servers
            ],
        }

    except Exception as e:
        logger.error(f"CLI status endpoint error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@cli_router.get("/providers")
async def list_cli_providers(session: AsyncSession = Depends(get_db_session)):
    """List providers with health information."""
    try:
        result = await session.execute(
            select(LLMServer)
        )
        servers = result.scalars().all()

        providers = []
        for server in servers:
            models_result = await session.execute(
                select(ServerModel).where(ServerModel.server_id == server.id)
            )
            server_models = models_result.scalars().all()

            model_names = []
            for m in server_models:
                name = m.frontend_model_name or m.actual_model_name
                model_names.append(name)

            providers.append({
                "name": _guess_provider(server.server_url),
                "server_url": server.server_url,
                "device": server.device or "",
                "models": model_names,
                "healthy": True,  # Would integrate with circuit breaker in Phase 10
            })

        return {"providers": providers, "count": len(providers)}

    except Exception as e:
        logger.error(f"CLI providers endpoint error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
