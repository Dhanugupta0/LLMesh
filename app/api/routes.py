import asyncio
import json
import os
import re
import time
from typing import Dict, Tuple
import httpx

from fastapi import APIRouter, HTTPException, Request, Response, Depends
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError
from pathlib import Path

from app.config.settings import settings
from app.middleware.auth import admin_required, verify_admin, user_required, verify_user
from app.models.api_models import ApiKeyUsage
from app.models.queue_models import UsageEventData, UsageEventType
from app.services.api_service import ApiService
from app.services.llm_service import LLMService
from app.utils.helpers import get_current_time, log_api_usage, estimate_tokens_fallback
from app.utils.logging_config import get_logger
from app.database.database import get_db_session
from app.database.repositories import (
    get_api_key_repo,
    ApiKeyRepository,
)
from app.utils.response_cache import response_cache
import bcrypt

logger = get_logger(__name__)

router = APIRouter()


async def _handle_llm_server_action(request, api_service, data, session: AsyncSession):
    """Core logic for handling LLM server operations"""
    action = data.get("action")
    url = data.get("url")
    config = data.get("config", {})
    model_status = data.get("status")

    # Decode the URL
    from urllib.parse import unquote

    url = unquote(url)

    # Handle the different actions
    if action == "add":
        # Add a new server - directly use the update_llm_server method
        # If the server already exists, update_llm_server updates it; otherwise it creates a new one
        await api_service.update_llm_server(url, config, session)
        await session.commit()
    elif action == "update":
        old_url = data.get("oldUrl")

        if old_url and old_url != url:
            # If the URL changed, delete the old server first, then add the new one
            # Check whether the new URL already exists
            servers_data = await api_service.load_llm_servers(session)
            if old_url in servers_data:
                # Delete the old server
                del servers_data[old_url]
            # Add/update the new server
            servers_data[url] = config
            # Use save_llm_servers to save all servers (includes a commit internally)
            await api_service.save_llm_servers(servers_data, session)
        else:
            # Only update the current server's configuration
            await api_service.update_llm_server(url, config, session)
            await session.commit()
    elif action == "delete":
        # Delete the server - load the existing servers, delete the specified one
        servers_data = await api_service.load_llm_servers(session)
        if url in servers_data:
            del servers_data[url]
            await api_service.save_llm_servers(servers_data, session)
    elif action == "toggle_status" and model_status is not None:
        # Toggle the model status - only update the status of a specific model
        model_id = data.get("model")
        if model_id:
            # Load the current server configuration
            servers_data = await api_service.load_llm_servers(session)
            if url in servers_data and model_id in servers_data[url].get("model", {}):
                # Only update this model's status, keeping all other config unchanged
                server_config = servers_data[url].copy()
                if "model" in server_config and model_id in server_config["model"]:
                    server_config["model"][model_id]["status"] = model_status
                    # Use update_llm_server to update only this server
                    await api_service.update_llm_server(url, server_config, session)
                    await session.commit()
    else:
        raise HTTPException(status_code=400, detail="Invalid action")

    llm_service = request.app.state.app.llm_service

    # Use a new session to read the data, avoiding stale data from the identity map
    from app.database.database import AsyncSessionLocal
    async with AsyncSessionLocal() as fresh_session:
        await llm_service.init_llm_resources_from_db(fresh_session)

    # Invalidate the model list cache
    llm_service.invalidate_models_cache()

    return {"status": "success"}


@router.get("/get-llm-servers")
@admin_required
async def get_llm_servers(
    request: Request, session: AsyncSession = Depends(get_db_session)
):
    """Get the LLM server list (requires admin privileges)"""
    try:
        _, api_service = get_services(request)
        servers_data = await api_service.load_llm_servers(session)
        return servers_data
    except HTTPException:
        raise
    except Exception as e:
        import logging
        logging.error(f"Error loading LLM servers: {e}", exc_info=True)
        raise HTTPException(
            status_code=500, detail=f"Error loading LLM servers: {str(e)}"
        )


@router.get("/circuit-breaker-stats")
@admin_required
async def get_circuit_breaker_stats(request: Request):
    """Get the circuit breaker states (requires admin privileges)

    Returns the circuit breaker state of every server, including:
    - Current state (closed/open/half_open)
    - Failure count
    - Last failure time
    - Total request and failure counts
    """
    try:
        llm_service, _ = get_services(request)
        return llm_service.get_circuit_breaker_stats()
    except Exception as e:
        import logging
        logging.error(f"Error getting circuit breaker stats: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Error getting circuit breaker stats: {str(e)}"
        )


@router.post("/reset-circuit-breaker")
@admin_required
async def reset_circuit_breaker(request: Request):
    """Reset the circuit breaker states (requires admin privileges)

    Used to manually recover servers that have been tripped.

    Request body:
    {
        "server_key": "api.example.com"  // optional; resets all when omitted
    }
    """
    try:
        llm_service, _ = get_services(request)
        data = await request.json() if "application/json" in request.headers.get("content-type", "") else {}
        server_key = data.get("server_key")

        await llm_service.reset_circuit_breaker(server_key)

        return {
            "status": "success",
            "message": f"Circuit breaker reset for {'all servers' if not server_key else server_key}"
        }
    except Exception as e:
        import logging
        logging.error(f"Error resetting circuit breaker: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Error resetting circuit breaker: {str(e)}"
        )


@router.post("/update-llm-servers")
@admin_required
async def update_llm_servers(
    request: Request, session: AsyncSession = Depends(get_db_session)
):
    """Update the LLM server list"""
    try:
        _, api_service = get_services(request)
        data = await request.json()
        return await _handle_llm_server_action(request, api_service, data, session)
    except HTTPException:
        raise
    except Exception as e:
        import logging
        logging.error(f"Error updating LLM servers: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Error updating LLM servers: {str(e)}",
            headers={"X-Error-Details": str(e)},
        )


@router.get("/models")
@router.get("/v1/models")
async def list_models(
    request: Request, session: AsyncSession = Depends(get_db_session)
):
    """Get the available models list - cache optimized for performance"""
    try:
        llm_service, _ = get_services(request)
        config = await llm_service.get_cached_models(session)

        models = []
        for server_url, server_info in config.items():
            device = server_info.get("device", "unknown")
            for model_id, model_info in server_info.get("model", {}).items():
                if model_info.get("status", False):
                    models.append(
                        {
                            "id": model_id,
                            "object": "model",
                            "owned_by": device,
                            "key": model_id,
                        }
                    )

        return {"object": "list", "data": models}
    except Exception as e:
        import logging
        logging.error(f"Error loading models: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error loading models: {str(e)}")


templates = Jinja2Templates(directory=settings.TEMPLATES_DIR)


def get_services(request: Request) -> tuple[LLMService, ApiService]:
    """Get the service instances"""
    app = request.app.state.app
    return app.llm_service, app.api_service


def get_usage_queue(request: Request):
    """Get the usage queue instance"""
    return request.app.state.app.usage_queue


def _estimate_tokens(api_service: ApiService, text: str) -> int:
    """Estimate the token count of a text

    Prefers tiktoken; falls back to the character-class estimation algorithm
    when the encoding is unavailable.
    """
    if not text:
        return 0
    if getattr(api_service, "_use_tiktoken", False) and getattr(api_service, "encoding", None):
        try:
            return len(api_service.encoding.encode(text))
        except Exception:
            pass
    return estimate_tokens_fallback(text)


def _estimate_prompt_tokens(api_service: ApiService, req_data: dict) -> int:
    """Estimate the prompt token count from the messages / prompt content in the request"""
    total = 0
    for m in req_data.get("messages") or []:
        content = m.get("content", "")
        if isinstance(content, str):
            total += _estimate_tokens(api_service, content)
        elif isinstance(content, list):
            # Multimodal message compatibility: only count the text parts
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    total += _estimate_tokens(api_service, part.get("text", ""))
    prompt = req_data.get("prompt")
    if isinstance(prompt, str):
        total += _estimate_tokens(api_service, prompt)
    elif isinstance(prompt, list):
        for p in prompt:
            if isinstance(p, str):
                total += _estimate_tokens(api_service, p)
    return total


def _parse_stream_chunks(raw_text: str):
    """Parse the accumulated SSE stream output

    Returns (output text, usage dict or None). Only parses complete data lines;
    incomplete trailing lines (when the client disconnects) are skipped by the
    JSON parse. If the upstream SSE final chunk carries a usage field
    (stream_options.include_usage), it is extracted as well.
    """
    text_parts = []
    usage = None
    for line in raw_text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        if data.get("usage"):
            usage = data["usage"]
        # OpenAI chat/completions streaming format
        for choice in data.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta")
            if isinstance(delta, dict) and isinstance(delta.get("content"), str):
                text_parts.append(delta["content"])
            if isinstance(choice.get("text"), str):
                text_parts.append(choice["text"])
    return "".join(text_parts), usage


async def _enqueue_stream_usage(
    usage_queue,
    api_service: ApiService,
    api_key: str,
    model: str,
    target_server: str,
    req_data: dict,
    collected_chunks: list,
    input_weight: float,
    output_weight: float,
):
    """Streaming request billing: prefer the upstream usage field; otherwise use tiktoken to estimate the request and accumulated output"""
    output_text, upstream_usage = _parse_stream_chunks("".join(collected_chunks))
    if upstream_usage:
        prompt_tokens = upstream_usage.get("prompt_tokens", 0)
        completion_tokens = upstream_usage.get("completion_tokens", 0)
    else:
        prompt_tokens = _estimate_prompt_tokens(api_service, req_data)
        completion_tokens = _estimate_tokens(api_service, output_text)

    await usage_queue.enqueue(
        UsageEventData(
            event_type=UsageEventType.UPDATE_USAGE,
            api_key=api_key,
            model=model,
            server_url=target_server,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            input_token_weight=input_weight,
            output_token_weight=output_weight,
        )
    )
    await usage_queue.enqueue(
        UsageEventData(
            event_type=UsageEventType.INCREMENT_MODEL_REQS,
            api_key=api_key,
            model=model,
            server_url=target_server,
        )
    )


@router.get("/get-config")
async def get_public_config():
    """Get the public configuration info"""
    return {
        "api_base_url": settings.API_BASE_URL,
        "chat_url": settings.CHAT_URL,
        "domain": settings.DOMAIN,
    }


@router.get("/get-models")
async def get_models(request: Request, session: AsyncSession = Depends(get_db_session)):
    """Get the available model list - cache optimized for performance"""
    try:
        llm_service, _ = get_services(request)
        config = await llm_service.get_cached_models(session)

        # Get all active models
        models = []
        for server_url, server_info in config.items():
            for model_id, model_info in server_info.get("model", {}).items():
                if model_info.get("status", False):
                    models.append(model_id)

        return {"models": models}

    except Exception as e:
        import logging
        logging.error(f"Error loading models: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error loading models: {str(e)}")


@router.get("/")
async def home():
    """Home page"""
    return FileResponse(os.path.join(settings.STATIC_DIR, "index.html"))



@router.get("/login")
async def login_page(request: Request):
    """Login page"""
    return templates.TemplateResponse(request, "login.html")


@router.post("/login")
async def login(request: Request):
    """Handle the login request"""
    # Supports two formats: JSON and form data
    content_type = request.headers.get("content-type", "")

    if "application/json" in content_type:
        data = await request.json()
        username = data.get("username")
        password = data.get("password")
        accept = request.headers.get("accept", "")
    else:
        # Form data format
        form_data = await request.form()
        username = form_data.get("username")
        password = form_data.get("password")
        accept = ""  # form submissions do not use JSON responses

    if verify_admin(username, password):
        request.session["authenticated"] = True
        request.session["is_admin"] = True
        if "application/json" in accept:
            return {"status": "success"}
        return RedirectResponse(url="/dashboard", status_code=303)

    # Login failed
    if "application/json" in accept:
        raise HTTPException(status_code=401, detail="Invalid username or password")
    else:
        # Form submission failed, return to the login page with the error
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "error": "Invalid username or password"
            }
        )


@router.get("/logout")
async def logout(request: Request):
    """Log out"""
    request.session.clear()
    return RedirectResponse(url="/")


# ========================================
# User authentication routes
# ========================================


@router.post("/user/register")
async def user_register(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    api_key_repo: ApiKeyRepository = Depends(get_api_key_repo),
):
    """User registration"""
    try:
        data = await request.json()
        phone = data.get("phone", "")
        password = data.get("password", "")

        if not phone or not password:
            raise HTTPException(status_code=400, detail="Phone number and password are required")

        if not re.match(r"^\+?\d{7,15}$", phone):
            raise HTTPException(status_code=400, detail="Please enter a valid phone number (e.g., +1234567890)")

        if len(password) < 6:
            raise HTTPException(status_code=400, detail="Password must be at least 6 characters")
        if len(password) > 72:
            raise HTTPException(status_code=400, detail="Password cannot exceed 72 characters")

        _, api_service = get_services(request)

        # Check whether the phone number already exists (friendly message on first check, IntegrityError as a backstop against concurrent races)
        existing_key = await api_key_repo.get_by_phone(phone)

        if existing_key:
            raise HTTPException(status_code=409, detail="This phone number is already registered")

        # Generate a new API key
        new_key = await api_service.generate_api_key(session)

        # Update the record, adding the phone number and password
        api_key_record = await api_key_repo.get_by_api_key(new_key)
        if api_key_record:
            api_key_record.phone = phone
            api_key_record.password_hash = bcrypt.hashpw(
                password.encode(), bcrypt.gensalt()
            ).decode()
            try:
                await session.commit()
            except IntegrityError:
                # Concurrent registration of the same phone number is handled by the database unique constraint
                await session.rollback()
                raise HTTPException(status_code=409, detail="This phone number is already registered")

        # Set the session
        request.session["user_authenticated"] = True
        request.session["user_phone"] = phone
        request.session["user_api_key"] = new_key

        return {"status": "success", "api_key": new_key}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"User registration failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Registration failed: {str(e)}")


@router.post("/user/login")
async def user_login(
    request: Request,
    api_key_repo: ApiKeyRepository = Depends(get_api_key_repo),
):
    """User login"""
    try:
        data = await request.json()
        phone = data.get("phone", "")
        password = data.get("password", "")

        if not phone or not password:
            raise HTTPException(status_code=400, detail="Phone number and password are required")

        if not re.match(r"^\+?\d{7,15}$", phone):
            raise HTTPException(status_code=400, detail="Please enter a valid phone number (e.g., +1234567890)")

        # Check whether the user exists
        api_key_record = await api_key_repo.get_by_phone(phone)

        if not api_key_record:
            raise HTTPException(status_code=404, detail="User does not exist")

        # Verify the password
        if not api_key_record.password_hash:
            raise HTTPException(status_code=401, detail="Account issue, please contact the administrator")

        if not verify_user(password, api_key_record.password_hash):
            raise HTTPException(status_code=401, detail="Incorrect password")

        # Set the session
        request.session["user_authenticated"] = True
        request.session["user_phone"] = phone
        request.session["user_api_key"] = api_key_record.api_key

        return {"status": "success", "redirect": "/user"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"User login failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Login failed: {str(e)}")


@router.get("/user/logout")
async def user_logout(request: Request):
    """User log out"""
    # Only clear the user-related session data
    request.session.pop("user_authenticated", None)
    request.session.pop("user_phone", None)
    request.session.pop("user_api_key", None)
    return RedirectResponse(url="/", status_code=303)


@router.get("/user/info")
async def get_user_info(
    request: Request,
    api_key_repo: ApiKeyRepository = Depends(get_api_key_repo),
):
    """Get the user info"""
    if not request.session.get("user_authenticated"):
        raise HTTPException(status_code=401, detail="Please log in first")

    phone = request.session.get("user_phone")
    if not phone:
        raise HTTPException(status_code=401, detail="Session expired, please log in again")

    api_key_record = await api_key_repo.get_by_phone_with_usages(phone)
    if not api_key_record:
        raise HTTPException(status_code=404, detail="User does not exist")

    # Get the model usage statistics
    model_usage = {}
    for mu in api_key_record.model_usages:
        model_usage[mu.model_name] = {
            "requests": mu.requests,
            "tokens": mu.tokens
        }

    return {
        "phone": api_key_record.phone,
        "api_key": api_key_record.api_key,
        "usage": api_key_record.usage or 0,
        "limit": api_key_record.limit_value or 1000000,
        "remaining": max(0, (api_key_record.limit_value or 1000000) - (api_key_record.usage or 0)),
        "reqs": api_key_record.reqs or 0,
        "created_at": api_key_record.created_at_str or (api_key_record.created_at.strftime("%Y-%m-%d %H:%M:%S") if api_key_record.created_at else None),
        "model_usage": model_usage,
    }


@router.get("/user", response_class=HTMLResponse)
@user_required
async def user_page(
    request: Request,
    api_key_repo: ApiKeyRepository = Depends(get_api_key_repo),
):
    """User page"""
    phone = request.session.get("user_phone")
    if not phone:
        return RedirectResponse(url="/", status_code=303)

    api_key_record = await api_key_repo.get_by_phone_with_usages(phone)
    if not api_key_record:
        request.session.clear()
        return RedirectResponse(url="/", status_code=303)

    # Get the model usage statistics
    model_usage = []
    for mu in api_key_record.model_usages:
        model_usage.append({
            "name": mu.model_name,
            "requests": mu.requests,
            "tokens": mu.tokens
        })

    return templates.TemplateResponse(
        request,
        "user.html",
        {
            "phone": api_key_record.phone,
            "api_key": api_key_record.api_key,
            "usage": api_key_record.usage or 0,
            "limit": api_key_record.limit_value or 1000000,
            "remaining": max(0, (api_key_record.limit_value or 1000000) - (api_key_record.usage or 0)),
            "reqs": api_key_record.reqs or 0,
            "created_at": api_key_record.created_at_str or (api_key_record.created_at.strftime("%Y-%m-%d %H:%M:%S") if api_key_record.created_at else "unknown"),
            "model_usage": model_usage,
            "current_time": get_current_time(),
        },
    )


@router.post("/generate-api-key")
async def generate_api_key(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    api_key_repo: ApiKeyRepository = Depends(get_api_key_repo),
):
    """Generate a new API key - supports phone number and password verification"""
    try:
        data = await request.json()
        phone = data.get("phone", "")
        password = data.get("password", "")

        if not phone or not password:
            raise HTTPException(status_code=400, detail="Phone number and password are required")

        if not re.match(r"^\+?\d{7,15}$", phone):
            raise HTTPException(status_code=400, detail="Please enter a valid phone number (e.g., +1234567890)")

        if len(password) < 6:
            raise HTTPException(status_code=400, detail="Password must be at least 6 characters")
        if len(password) > 72:
            raise HTTPException(status_code=400, detail="Password cannot exceed 72 characters")

        _, api_service = get_services(request)

        # Check whether the phone number already exists (friendly message on first check, IntegrityError as a backstop against concurrent races)
        existing_key = await api_key_repo.get_by_phone(phone)

        if existing_key:
            # If the phone number already exists, tell the user it is registered
            raise HTTPException(status_code=409, detail="This phone number is already registered")

        # Generate a new API key
        new_key = await api_service.generate_api_key(session)

        # Update the record, adding the phone number and password
        api_key_record = await api_key_repo.get_by_api_key(new_key)
        if api_key_record:
            api_key_record.phone = phone
            api_key_record.password_hash = bcrypt.hashpw(
                password.encode(), bcrypt.gensalt()
            ).decode()
            try:
                await session.commit()
            except IntegrityError:
                # Concurrent registration of the same phone number is handled by the database unique constraint
                await session.rollback()
                raise HTTPException(status_code=409, detail="This phone number is already registered")

        return {"api_key": new_key}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to generate API key: {str(e)}")


@router.post("/check-usage")
async def check_usage(
    request: Request,
    api_key_repo: ApiKeyRepository = Depends(get_api_key_repo),
):
    """Check the API key usage quota"""
    try:
        data = await request.json()
        phone = data.get("phone", "")
        password = data.get("password", "")

        if not phone or not password:
            raise HTTPException(status_code=400, detail="Phone number and password are required")

        if not re.match(r"^\+?\d{7,15}$", phone):
            raise HTTPException(status_code=400, detail="Please enter a valid phone number (e.g., +1234567890)")

        # Check whether the phone number already exists
        existing_key = await api_key_repo.get_by_phone(phone)

        if not existing_key:
            raise HTTPException(status_code=404, detail="No account found for this phone number")

        # Verify the password - uses bcrypt verification
        try:
            if not bcrypt.checkpw(
                password.encode(), existing_key.password_hash.encode()
            ):
                return JSONResponse(
                    status_code=401, content={"error": "Incorrect password", "detail": "Incorrect password"}
                )
        except Exception:
            return JSONResponse(
                status_code=401, content={"error": "Incorrect password", "detail": "Incorrect password"}
            )

        # Return the usage quota info
        usage = existing_key.usage or 0
        limit = existing_key.limit_value or 1000000  # default limit of 1,000,000 tokens
        remaining = max(0, limit - usage)

        return {
            "api_key": existing_key.api_key,
            "usage": usage,
            "limit": limit,
            "remaining": remaining,
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to check usage: {str(e)}")


@router.post("/update-api-key-limit")
@admin_required
async def update_api_key_limit(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    api_key_repo: ApiKeyRepository = Depends(get_api_key_repo),
):
    """Update the usage limit of an API key"""
    data = await request.json()
    api_key = data.get("api_key")
    new_limit = data.get("new_limit")

    if not api_key or new_limit is None:
        raise HTTPException(
            status_code=400, detail="API key and new limit are required"
        )

    # Use the Repository to update the limit
    success = await api_key_repo.update_limit(api_key, new_limit)
    if not success:
        raise HTTPException(status_code=404, detail="API key not found")

    # Commit the transaction
    await session.commit()

    # Invalidate the API Key cache
    _, api_service = get_services(request)
    await api_service.invalidate_api_key_cache(api_key)

    return {"status": "success"}


@router.post("/reset-api-key-usage")
@admin_required
async def reset_api_key_usage(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    api_key_repo: ApiKeyRepository = Depends(get_api_key_repo),
):
    """Reset the usage of an API key"""
    data = await request.json()
    api_key = data.get("api_key")
    if not api_key:
        raise HTTPException(status_code=400, detail="API key is required")

    # Use the Repository to reset the usage
    success = await api_key_repo.reset_usage(api_key)
    if not success:
        raise HTTPException(status_code=404, detail="API key not found")

    # Commit the transaction
    await session.commit()

    # Invalidate the API Key cache
    _, api_service = get_services(request)
    await api_service.invalidate_api_key_cache(api_key)

    return {"status": "success"}


@router.post("/revoke-api-key")
@admin_required
async def revoke_api_key(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    api_key_repo: ApiKeyRepository = Depends(get_api_key_repo),
):
    """Revoke an API key"""
    data = await request.json()
    api_key = data.get("api_key")
    if not api_key:
        raise HTTPException(status_code=400, detail="API key is required")

    # Use the Repository to delete the API key
    success = await api_key_repo.delete_by_api_key(api_key)
    if not success:
        raise HTTPException(status_code=404, detail="API key not found")

    # Commit the transaction
    await session.commit()

    # Invalidate the API Key cache
    _, api_service = get_services(request)
    await api_service.invalidate_api_key_cache(api_key)

    return {"status": "success"}


@router.post("/change-user-password")
@admin_required
async def change_user_password(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    api_key_repo: ApiKeyRepository = Depends(get_api_key_repo),
):
    """Admin changes a user's password"""
    try:
        data = await request.json()
        api_key = data.get("api_key")
        new_password = data.get("new_password")

        if not api_key or not new_password:
            raise HTTPException(
                status_code=400, detail="API key and new password are required"
            )

        if len(new_password) < 6:
            raise HTTPException(status_code=400, detail="Password must be at least 6 characters")
        if len(new_password) > 72:
            raise HTTPException(status_code=400, detail="Password cannot exceed 72 characters")

        # Check whether the API key exists
        api_key_record = await api_key_repo.get_by_api_key(api_key)

        if not api_key_record:
            raise HTTPException(status_code=404, detail="No user found for this API key")

        # Update the password hash
        api_key_record.password_hash = bcrypt.hashpw(
            new_password.encode(), bcrypt.gensalt()
        ).decode()
        await session.commit()

        return {"status": "success", "message": "Password changed successfully"}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to change password: {str(e)}")


@router.get("/dashboard", response_class=HTMLResponse)
@admin_required
async def usage_dashboard(
    request: Request,
    api_key_repo: ApiKeyRepository = Depends(get_api_key_repo),
):
    """Usage statistics and management dashboard"""
    # Use the Repository to fetch the data, including the model usage statistics
    api_keys_data = await api_key_repo.get_all_with_usages()

    # Compute the statistics
    total_usage = sum(key.usage or 0 for key in api_keys_data)
    total_entries = len(api_keys_data)
    total_reqs = sum(key.reqs or 0 for key in api_keys_data)

    # Count the number of keys in each usage bracket
    less_than_100 = sum(1 for key in api_keys_data if (key.usage or 0) < 100)
    between_100_and_10000 = sum(
        1 for key in api_keys_data if 100 <= (key.usage or 0) < 10000
    )
    more_than_10000 = sum(1 for key in api_keys_data if (key.usage or 0) >= 10000)

    # Build the API key list
    api_keys = []
    for key in api_keys_data:
        # Use the to_dict method to get the full data, including model_usage
        key_data = key.to_dict()
        key_data["key"] = key.api_key
        api_keys.append(key_data)

    return templates.TemplateResponse(
        request,
        "dashboard_manage.html",
        {
            "total_usage": total_usage,
            "total_entries": total_entries,
            "total_reqs": total_reqs,
            "less_than_100": less_than_100,
            "between_100_and_10000": between_100_and_10000,
            "more_than_10000": more_than_10000,
            "api_keys": api_keys,
            "current_time": get_current_time(),
        },
    )


# ========================================
# Client downloads
# ========================================

DOWNLOAD_DIR = Path(__file__).parent.parent.parent / "downloads"


@router.get("/downloads")
async def list_downloads():
    """Get the downloadable client list"""
    available_files = []

    # Check for macOS clients
    mac_files = list(DOWNLOAD_DIR.glob("*.dmg")) + list(DOWNLOAD_DIR.glob("*.pkg"))
    for f in mac_files:
        available_files.append({
            "platform": "macOS",
            "filename": f.name,
            "url": f"/download/{f.name}",
            "size": f.stat().st_size if f.exists() else 0
        })

    # Check for Windows clients
    win_files = list(DOWNLOAD_DIR.glob("*.exe")) + list(DOWNLOAD_DIR.glob("*.zip"))
    for f in win_files:
        available_files.append({
            "platform": "Windows",
            "filename": f.name,
            "url": f"/download/{f.name}",
            "size": f.stat().st_size if f.exists() else 0
        })

    return {"files": available_files}


@router.get("/download/{filename}")
async def download_client(filename: str):
    """Safely download a client file"""
    # Security check: ensure the filename contains no path traversal characters
    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    file_path = DOWNLOAD_DIR / filename

    # Check whether the file exists
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    # Check whether the file extension is allowed
    allowed_extensions = {".dmg", ".pkg", ".exe", ".zip"}
    if file_path.suffix.lower() not in allowed_extensions:
        raise HTTPException(status_code=403, detail="File type not allowed")

    # Set the appropriate Content-Type based on the platform
    content_type = "application/octet-stream"
    if filename.endswith(".dmg"):
        content_type = "application/x-apple-diskimage"
    elif filename.endswith(".pkg"):
        content_type = "application/vnd.apple.installer+xml"
    elif filename.endswith(".exe"):
        content_type = "application/vnd.microsoft.portable-executable"
    elif filename.endswith(".zip"):
        content_type = "application/zip"

    return FileResponse(
        path=str(file_path),
        filename=filename,
        media_type=content_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
        }
    )


@router.options("/v1/chat/completions")
@router.options("/chat/completions")
@router.options("/v1/completions")
@router.options("/completions")
async def options_handler():
    """Handle OPTIONS requests"""
    return Response(status_code=200)


@router.post("/v1/chat/completions")
@router.post("/chat/completions")
async def proxy_handler_chat(
    request: Request, session: AsyncSession = Depends(get_db_session)
):
    """Request forwarding handler"""
    llm_service, api_service = get_services(request)
    usage_queue = get_usage_queue(request)

    # Authentication
    auth_header = request.headers.get("Authorization", "")
    _, _, api_key = auth_header.partition(" ")

    await api_service.validate_api_key(api_key, session)
    await api_service.check_usage_limit(api_key, session)

    # Request handling
    req_data = await request.json()
    model = req_data.get("model")

    # Get the target server
    target_server = llm_service.get_target_server(model)
    target = f"{target_server}{request.url.path.replace('/v1', '', 1)}"

    # Build the request headers
    headers = llm_service.get_auth_header(model, api_key, target_server)

    try:
        # Streaming response handling
        if req_data.get("stream", False):
            # Get the model weights before streaming starts, to avoid using a closed session after the stream ends
            input_weight, output_weight = await api_service._get_model_weights(model, session, target_server)

            async def stream_wrapper():
                start_time = time.time()
                chunk_count = 0
                max_retries = 1
                has_yielded = False  # whether content has been sent to the client (determines whether a retry is allowed after a disconnect)
                should_bill = False  # the upstream stream was established normally (2xx); billing is required on finish or disconnect
                collected_chunks = []  # accumulated upstream output, used to estimate tokens after the stream ends
                server_key = llm_service._extract_server_key(target)

                try:
                    for attempt in range(max_retries + 1):
                        try:
                            client_stream = await llm_service.forward_request(
                                target, req_data, headers, stream=True
                            )

                            async with client_stream as response:
                                # Upstream returned an error status: record the circuit failure, pass through the error and stop (no billing, no counting)
                                if response.status_code >= 400:
                                    error_body = await response.aread()
                                    await llm_service.circuit_breaker.record_failure(server_key)
                                    llm_service._update_server_health(server_key, False)
                                    logger.warning(
                                        f"stream upstream error | model={model} | status={response.status_code}"
                                    )
                                    error_data = {
                                        "error": {
                                            "message": f"Upstream service returned an error (status code {response.status_code})",
                                            "type": "upstream_error",
                                            "code": response.status_code,
                                        }
                                    }
                                    yield f"data: {json.dumps(error_data)}\n\n"
                                    yield "data: [DONE]\n\n"
                                    return

                                should_bill = True
                                first_chunk_time = None
                                async for chunk in response.aiter_text():
                                    if first_chunk_time is None:
                                        first_chunk_time = time.time()
                                        first_chunk_delay = first_chunk_time - start_time
                                        logger.debug(f"First chunk | model={model} | delay={first_chunk_delay:.3f}s")

                                    chunk_count += 1
                                    collected_chunks.append(chunk)
                                    has_yielded = True
                                    yield chunk

                            # Stream completed normally, record the success in the circuit breaker
                            await llm_service.circuit_breaker.record_success(server_key)
                            llm_service._update_server_health(server_key, True)

                            end_time = time.time()
                            total_duration = end_time - start_time

                            # Log the streaming response performance metrics (first_chunk shows N/A with zero chunks)
                            first_chunk_str = (
                                f"{first_chunk_delay:.3f}s" if first_chunk_time is not None else "N/A"
                            )
                            logger.info(
                                f"Stream completed | model={model} | "
                                f"duration={total_duration:.3f}s | chunks={chunk_count} | "
                                f"first_chunk={first_chunk_str}"
                            )
                            break  # completed successfully, exit the retry loop

                        except httpx.RemoteProtocolError as exc:
                            await llm_service.circuit_breaker.record_failure(server_key)
                            llm_service._update_server_health(server_key, False)
                            logger.warning(
                                f"Stream connection error (attempt {attempt + 1}/{max_retries + 1}) model={model}: {exc}"
                            )
                            # Only allow a retry when nothing has been sent to the client yet, to avoid duplicate pushes
                            if attempt < max_retries and not has_yielded:
                                await asyncio.sleep(0.5)
                                continue
                            error_data = {
                                "error": {
                                    "message": f"Upstream service connection interrupted: {str(exc)}",
                                    "type": "connection_error",
                                    "code": "connection_terminated"
                                }
                            }
                            yield f"data: {json.dumps(error_data)}\n\n"
                            yield "data: [DONE]\n\n"
                            break

                        except Exception as exc:
                            # HTTPException (e.g. circuit breaker open) is already recorded upstream; avoid double counting
                            if not isinstance(exc, HTTPException):
                                await llm_service.circuit_breaker.record_failure(server_key)
                                llm_service._update_server_health(server_key, False)
                            logger.error(f"Stream error model={model}: {exc}")
                            error_data = {
                                "error": {
                                    "message": f"Streaming response error: {str(exc)}",
                                    "type": "stream_error"
                                }
                            }
                            yield f"data: {json.dumps(error_data)}\n\n"
                            yield "data: [DONE]\n\n"
                            break
                finally:
                    # Whether the stream ended normally or the client disconnected
                    # (GeneratorExit/cancel), bill by the produced content as long
                    # as the upstream stream was established
                    if should_bill:
                        try:
                            await _enqueue_stream_usage(
                                usage_queue, api_service, api_key, model,
                                target_server, req_data, collected_chunks,
                                input_weight, output_weight,
                            )
                        except Exception as e:
                            logger.warning(f"Failed to enqueue streaming billing model={model}: {e}")
                    else:
                        # Upstream error produced no billing, release the reserved concurrent quota
                        await api_service.release_in_flight(api_key)

            return StreamingResponse(
                stream_wrapper(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",  # disable Nginx buffering
                },
            )

        # Normal response handling
        response_text = await llm_service.forward_request(target, req_data, headers)

        try:
            response = json.loads(response_text)

            # Get the model weights
            input_weight, output_weight = await api_service._get_model_weights(model, session, target_server)

            # Enqueue the statistics events
            if "usage" in response:
                await usage_queue.enqueue(
                    UsageEventData(
                        event_type=UsageEventType.UPDATE_USAGE,
                        api_key=api_key,
                        model=model,
                        server_url=target_server,
                        prompt_tokens=response["usage"].get("prompt_tokens", 0),
                        completion_tokens=response["usage"].get("completion_tokens", 0),
                        input_token_weight=input_weight,
                        output_token_weight=output_weight,
                    )
                )

            # Enqueue the model request count event
            await usage_queue.enqueue(
                UsageEventData(
                    event_type=UsageEventType.INCREMENT_MODEL_REQS,
                    api_key=api_key,
                    model=model,
                    server_url=target_server,
                )
            )

            return JSONResponse(response)
        except json.JSONDecodeError as e:
            await api_service.release_in_flight(api_key)
            return JSONResponse(
                {"error": "Invalid response from upstream server", "message": str(e)},
                status_code=500,
            )

    except HTTPException as e:
        await api_service.release_in_flight(api_key)
        return JSONResponse({"error": str(e.detail)}, status_code=e.status_code)
    except Exception as e:
        await api_service.release_in_flight(api_key)
        logger.error(f"chat/completions error: {e}", exc_info=True)
        return JSONResponse({"error": "Internal server error"}, status_code=500)


@router.post("/v1/embeddings")
@router.post("/embeddings")
async def proxy_handler_embeddings(
    request: Request, session: AsyncSession = Depends(get_db_session)
):
    """Handle embeddings request forwarding (with response caching)"""
    llm_service, api_service = get_services(request)
    usage_queue = get_usage_queue(request)

    # Authentication
    auth_header = request.headers.get("Authorization", "")
    _, _, api_key = auth_header.partition(" ")
    await api_service.validate_api_key(api_key, session)
    await api_service.check_usage_limit(api_key, session)

    # Request handling
    req_data = await request.json()
    model = req_data.get("model")

    # Try to get from the cache (only enabled for non-streaming requests)
    if not req_data.get("stream", False):
        cached_response = await response_cache.get(req_data)
        if cached_response:
            logger.debug(f"embeddings cache hit | model={model}")
            # Update the usage statistics (must be recorded even on a cache hit)
            target_server = llm_service.get_target_server(model)
            input_weight, output_weight = await api_service._get_model_weights(model, session, target_server)
            if "usage" in cached_response and "prompt_tokens" in cached_response["usage"]:
                await usage_queue.enqueue(
                    UsageEventData(
                        event_type=UsageEventType.UPDATE_USAGE,
                        api_key=api_key,
                        model=model,
                        server_url=target_server,
                        prompt_tokens=cached_response["usage"]["prompt_tokens"],
                        completion_tokens=0,
                        input_token_weight=input_weight,
                        output_token_weight=output_weight,
                    )
                )
            await usage_queue.enqueue(
                UsageEventData(
                    event_type=UsageEventType.INCREMENT_MODEL_REQS,
                    api_key=api_key,
                    model=model,
                    server_url=target_server,
                )
            )
            return JSONResponse(cached_response)

    # Get the target server
    target_server = llm_service.get_target_server(model)
    target = f"{target_server}{request.url.path.replace('/v1', '', 1)}"

    # Build the request headers
    headers = llm_service.get_auth_header(model, api_key, target_server)

    try:
        # Forward the request
        response_text = await llm_service.forward_request(target, req_data, headers)
        response = json.loads(response_text)

        # Cache the response (only non-streaming responses without errors)
        if not req_data.get("stream", False) and "error" not in response:
            await response_cache.set(req_data, response)
            logger.debug(f"embeddings cache set | model={model}")

        # Get the model weights
        input_weight, output_weight = await api_service._get_model_weights(model, session, target_server)

        # Update the usage - the embeddings endpoint only has prompt_tokens
        if "usage" in response and "prompt_tokens" in response["usage"]:
            await usage_queue.enqueue(
                UsageEventData(
                    event_type=UsageEventType.UPDATE_USAGE,
                    api_key=api_key,
                    model=model,
                    server_url=target_server,
                    prompt_tokens=response["usage"]["prompt_tokens"],
                    completion_tokens=0,  # embeddings has no completion_tokens
                    input_token_weight=input_weight,
                    output_token_weight=output_weight,
                )
            )

        # Enqueue the model request count event
        await usage_queue.enqueue(
            UsageEventData(
                event_type=UsageEventType.INCREMENT_MODEL_REQS,
                api_key=api_key,
                model=model,
                server_url=target_server,
            )
        )

        return JSONResponse(response)

    except json.JSONDecodeError as e:
        return JSONResponse(
            {"error": "Invalid response from upstream server", "message": str(e)},
            status_code=500,
        )
    except HTTPException as e:
        await api_service.release_in_flight(api_key)
        return JSONResponse({"error": str(e.detail)}, status_code=e.status_code)
    except Exception as e:
        await api_service.release_in_flight(api_key)
        logger.error(f"embeddings error: {e}", exc_info=True)
        return JSONResponse({"error": "Internal server error"}, status_code=500)


@router.post("/v1/completions")
@router.post("/completions")
async def proxy_handler_completions(
    request: Request, session: AsyncSession = Depends(get_db_session)
):
    """Request forwarding handler"""
    llm_service, api_service = get_services(request)
    usage_queue = get_usage_queue(request)

    # Authentication
    auth_header = request.headers.get("Authorization", "")
    _, _, api_key = auth_header.partition(" ")
    await api_service.validate_api_key(api_key, session)
    await api_service.check_usage_limit(api_key, session)

    # Request handling
    req_data = await request.json()
    model = req_data.get("model")

    # Get the target server
    target_server = llm_service.get_target_server(model)
    target = f"{target_server}{request.url.path.replace('/v1', '', 1)}"

    # Build the request headers
    headers = llm_service.get_auth_header(model, api_key, target_server)

    try:
        # Streaming response handling
        if req_data.get("stream", False):
            # Get the model weights before streaming starts, to avoid using a closed session after the stream ends
            input_weight, output_weight = await api_service._get_model_weights(model, session, target_server)

            async def stream_wrapper():
                max_retries = 1
                has_yielded = False  # whether content has been sent to the client (determines whether a retry is allowed after a disconnect)
                should_bill = False  # the upstream stream was established normally (2xx); billing is required on finish or disconnect
                collected_chunks = []  # accumulated upstream output, used to estimate tokens after the stream ends
                server_key = llm_service._extract_server_key(target)

                try:
                    for attempt in range(max_retries + 1):
                        try:
                            client_stream = await llm_service.forward_request(
                                target, req_data, headers, stream=True
                            )

                            async with client_stream as response:
                                # Upstream returned an error status: record the circuit failure, pass through the error and stop (no billing, no counting)
                                if response.status_code >= 400:
                                    error_body = await response.aread()
                                    await llm_service.circuit_breaker.record_failure(server_key)
                                    llm_service._update_server_health(server_key, False)
                                    logger.warning(
                                        f"stream upstream error | model={model} | status={response.status_code}"
                                    )
                                    error_data = {
                                        "error": {
                                            "message": f"Upstream service returned an error (status code {response.status_code})",
                                            "type": "upstream_error",
                                            "code": response.status_code,
                                        }
                                    }
                                    yield f"data: {json.dumps(error_data)}\n\n"
                                    yield "data: [DONE]\n\n"
                                    return

                                should_bill = True
                                async for chunk in response.aiter_text():
                                    collected_chunks.append(chunk)
                                    has_yielded = True
                                    yield chunk

                            # Stream completed normally, record the success in the circuit breaker
                            await llm_service.circuit_breaker.record_success(server_key)
                            llm_service._update_server_health(server_key, True)
                            break  # completed successfully

                        except httpx.RemoteProtocolError as exc:
                            await llm_service.circuit_breaker.record_failure(server_key)
                            llm_service._update_server_health(server_key, False)
                            logger.warning(
                                f"Stream connection error (attempt {attempt + 1}/{max_retries + 1}) model={model}: {exc}"
                            )
                            # Only allow a retry when nothing has been sent to the client yet, to avoid duplicate pushes
                            if attempt < max_retries and not has_yielded:
                                await asyncio.sleep(0.5)
                                continue
                            error_data = {
                                "error": {
                                    "message": f"Upstream service connection interrupted: {str(exc)}",
                                    "type": "connection_error",
                                    "code": "connection_terminated"
                                }
                            }
                            yield f"data: {json.dumps(error_data)}\n\n"
                            yield "data: [DONE]\n\n"
                            break

                        except Exception as exc:
                            # HTTPException (e.g. circuit breaker open) is already recorded upstream; avoid double counting
                            if not isinstance(exc, HTTPException):
                                await llm_service.circuit_breaker.record_failure(server_key)
                                llm_service._update_server_health(server_key, False)
                            logger.error(f"Stream error model={model}: {exc}")
                            error_data = {
                                "error": {
                                    "message": f"Streaming response error: {str(exc)}",
                                    "type": "stream_error"
                                }
                            }
                            yield f"data: {json.dumps(error_data)}\n\n"
                            yield "data: [DONE]\n\n"
                            break
                finally:
                    # Whether the stream ended normally or the client disconnected
                    # (GeneratorExit/cancel), bill by the produced content as long
                    # as the upstream stream was established
                    if should_bill:
                        try:
                            await _enqueue_stream_usage(
                                usage_queue, api_service, api_key, model,
                                target_server, req_data, collected_chunks,
                                input_weight, output_weight,
                            )
                        except Exception as e:
                            logger.warning(f"Failed to enqueue streaming billing model={model}: {e}")
                    else:
                        # Upstream error produced no billing, release the reserved concurrent quota
                        await api_service.release_in_flight(api_key)

            return StreamingResponse(
                stream_wrapper(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",  # disable Nginx buffering
                },
            )

        # Normal response handling
        response_text = await llm_service.forward_request(target, req_data, headers)

        try:
            response = json.loads(response_text)

            # Get the model weights
            input_weight, output_weight = await api_service._get_model_weights(model, session, target_server)

            # Enqueue the statistics events
            if "usage" in response:
                await usage_queue.enqueue(
                    UsageEventData(
                        event_type=UsageEventType.UPDATE_USAGE,
                        api_key=api_key,
                        model=model,
                        server_url=target_server,
                        prompt_tokens=response["usage"].get("prompt_tokens", 0),
                        completion_tokens=response["usage"].get("completion_tokens", 0),
                        input_token_weight=input_weight,
                        output_token_weight=output_weight,
                    )
                )

            # Enqueue the model request count event
            await usage_queue.enqueue(
                UsageEventData(
                    event_type=UsageEventType.INCREMENT_MODEL_REQS,
                    api_key=api_key,
                    model=model,
                    server_url=target_server,
                )
            )

            return JSONResponse(response)
        except json.JSONDecodeError as e:
            await api_service.release_in_flight(api_key)
            return JSONResponse(
                {"error": "Invalid response from upstream server", "message": str(e)},
                status_code=500,
            )

    except HTTPException as e:
        await api_service.release_in_flight(api_key)
        return JSONResponse({"error": str(e.detail)}, status_code=e.status_code)
    except Exception as e:
        await api_service.release_in_flight(api_key)
        logger.error(f"completions error: {e}", exc_info=True)
        return JSONResponse({"error": "Internal server error"}, status_code=500)


@router.options("/anthropic")
@router.options("/anthropic/v1/messages")
async def anthropic_options_handler():
    """Handle /anthropic OPTIONS requests"""
    return Response(status_code=200)


@router.post("/anthropic")
@router.post("/anthropic/v1/messages")
async def anthropic_proxy_handler(
    request: Request, session: AsyncSession = Depends(get_db_session)
):
    """Anthropic API forwarding handler - computes usage by request count and model weights"""
    llm_service, api_service = get_services(request)
    usage_queue = get_usage_queue(request)

    # Authentication - prefer Authorization: Bearer, then x-api-key
    api_key = ""
    auth_header = request.headers.get("Authorization", "")

    if auth_header.startswith("Bearer "):
        _, _, api_key = auth_header.partition(" ")

    # If there is no Authorization header, try x-api-key
    if not api_key:
        api_key = request.headers.get("x-api-key", "")

    if not api_key:
        raise HTTPException(status_code=401, detail="Invalid API Key")

    # Validate the API key
    await api_service.validate_api_key(api_key, session)
    await api_service.check_usage_limit(api_key, session)

    # Request handling
    req_data = await request.json()
    model = req_data.get("model")

    # Get the target server
    target_server = llm_service.get_target_server(model)

    # Get the model weights (for usage computation)
    input_weight, output_weight = await api_service._get_model_weights(model, session, target_server)

    # Log the request
    masked_key = f"{api_key[:8]}...{api_key[-4:]}"
    logger.info(f"anthropic request | model={model} | key={masked_key} | server={target_server}")

    # Build the target URL - both the bare /anthropic path and /anthropic/v1/messages forward to /v1/messages
    target = f"{target_server}/v1/messages"

    # Build the request headers - provide both x-api-key and Authorization: Bearer authentication
    upstream_api_key = api_key
    if model in llm_service.app_state.cloud_models:
        server_mappings = llm_service.app_state.cloud_models[model]
        upstream_api_key = server_mappings.get(target_server, api_key)

    # Prefer passing through the client's anthropic-version, otherwise use the configured default
    client_anthropic_version = request.headers.get("anthropic-version", "")

    headers = {
        "x-api-key": upstream_api_key,
        "Authorization": f"Bearer {upstream_api_key}",
        "Content-Type": "application/json",
        "anthropic-version": client_anthropic_version or settings.ANTHROPIC_VERSION,
    }

    try:
        # Streaming response handling
        if req_data.get("stream", False):
            async def stream_wrapper():
                max_retries = 1
                has_yielded = False  # whether content has been sent to the client (determines whether a retry is allowed after a disconnect)
                should_bill = False  # the upstream stream was established normally (2xx); billing is required on finish or disconnect
                server_key = llm_service._extract_server_key(target)

                try:
                    for attempt in range(max_retries + 1):
                        try:
                            client_stream = await llm_service.forward_request(
                                target, req_data, headers, stream=True
                            )

                            async with client_stream as response:
                                # Upstream returned an error status: record the circuit failure, pass through the error and stop (no billing, no counting)
                                if response.status_code >= 400:
                                    error_body = await response.aread()
                                    await llm_service.circuit_breaker.record_failure(server_key)
                                    llm_service._update_server_health(server_key, False)
                                    logger.warning(
                                        f"anthropic stream upstream error | model={model} | status={response.status_code}"
                                    )
                                    yield f'event: error\n'
                                    yield f'data: {json.dumps({"error": {"message": f"Upstream service returned an error (status code {response.status_code})", "type": "upstream_error"}})}\n\n'
                                    yield 'event: message_stop\n'
                                    yield 'data: {"type": "message_stop"}\n\n'
                                    return

                                should_bill = True
                                async for chunk in response.aiter_text():
                                    has_yielded = True
                                    yield chunk

                            # Streaming response completed successfully, record the success in the circuit breaker
                            await llm_service.circuit_breaker.record_success(server_key)
                            llm_service._update_server_health(server_key, True)
                            logger.info(f"anthropic stream completed | model={model} | key={masked_key}")
                            break  # completed successfully

                        except httpx.RemoteProtocolError as exc:
                            await llm_service.circuit_breaker.record_failure(server_key)
                            llm_service._update_server_health(server_key, False)
                            logger.warning(
                                f"Stream connection error (attempt {attempt + 1}/{max_retries + 1}) model={model}: {exc}"
                            )
                            # Only allow a retry when nothing has been sent to the client yet, to avoid duplicate pushes
                            if attempt < max_retries and not has_yielded:
                                await asyncio.sleep(0.5)
                                continue
                            # Anthropic error event format
                            yield f'event: error\n'
                            yield f'data: {json.dumps({"error": {"message": f"Upstream service connection interrupted: {str(exc)}", "type": "connection_error"}})}\n\n'
                            yield 'event: message_stop\n'
                            yield 'data: {"type": "message_stop"}\n\n'
                            break

                        except Exception as exc:
                            # HTTPException (e.g. circuit breaker open) is already recorded upstream; avoid double counting
                            if not isinstance(exc, HTTPException):
                                await llm_service.circuit_breaker.record_failure(server_key)
                                llm_service._update_server_health(server_key, False)
                            logger.error(f"Stream error model={model}: {exc}")
                            yield f'event: error\n'
                            yield f'data: {json.dumps({"error": {"message": f"Streaming response error: {str(exc)}", "type": "stream_error"}})}\n\n'
                            yield 'event: message_stop\n'
                            yield 'data: {"type": "message_stop"}\n\n'
                            break
                finally:
                    # Bill as long as the upstream stream was established, whether it
                    # ended normally or the client disconnected
                    if should_bill:
                        try:
                            await usage_queue.enqueue(
                                UsageEventData(
                                    event_type=UsageEventType.UPDATE_ANTHROPIC_USAGE,
                                    api_key=api_key,
                                    model=model,
                                    input_token_weight=input_weight,
                                    output_token_weight=output_weight,
                                )
                            )
                            await usage_queue.enqueue(
                                UsageEventData(
                                    event_type=UsageEventType.INCREMENT_MODEL_REQS,
                                    api_key=api_key,
                                    model=model,
                                    server_url=target_server,
                                )
                            )
                        except Exception as e:
                            logger.warning(f"Failed to enqueue anthropic streaming billing model={model}: {e}")
                    else:
                        # Upstream error produced no billing, release the reserved concurrent quota
                        await api_service.release_in_flight(api_key)

            return StreamingResponse(
                stream_wrapper(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",  # disable Nginx buffering
                },
            )

        # Normal response handling
        response_text = await llm_service.forward_request(target, req_data, headers)

        try:
            response = json.loads(response_text)

            # Normal response succeeded
            logger.info(f"anthropic response completed | model={model} | key={masked_key}")

            # Enqueue the statistics events - Anthropic computes usage by request count and weights
            await usage_queue.enqueue(
                UsageEventData(
                    event_type=UsageEventType.UPDATE_ANTHROPIC_USAGE,
                    api_key=api_key,
                    model=model,
                    input_token_weight=input_weight,
                    output_token_weight=output_weight,
                )
            )
            await usage_queue.enqueue(
                UsageEventData(
                    event_type=UsageEventType.INCREMENT_MODEL_REQS,
                    api_key=api_key,
                    model=model,
                    server_url=target_server,
                )
            )

            return JSONResponse(response)
        except json.JSONDecodeError as e:
            await api_service.release_in_flight(api_key)
            return JSONResponse(
                {"error": "Invalid response from upstream server", "message": str(e)},
                status_code=500,
            )

    except HTTPException as e:
        await api_service.release_in_flight(api_key)
        return JSONResponse({"error": str(e.detail)}, status_code=e.status_code)
    except Exception as e:
        await api_service.release_in_flight(api_key)
        logger.error(f"anthropic error: {e}", exc_info=True)
        return JSONResponse({"error": "Internal server error"}, status_code=500)


# ========================================
# Coding endpoint - OpenAI format + Anthropic usage statistics
# ========================================


@router.options("/coding")
@router.options("/coding/chat/completions")
async def coding_options_handler():
    """Handle /coding OPTIONS requests"""
    return Response(status_code=200)


@router.post("/coding")
@router.post("/coding/chat/completions")
async def coding_proxy_handler(
    request: Request, session: AsyncSession = Depends(get_db_session)
):
    """Coding API forwarding handler - OpenAI format requests + Anthropic usage statistics

    Features:
    - Request format: compatible with /v1/chat/completions
    - Usage statistics: requests x weight, no token counting (same as /anthropic)
    - Use case: OpenAI-compatible APIs billed per request, such as Zhipu AI

    Usage formula:
    usage per request = max(input_token_weight, output_token_weight)
    """
    llm_service, api_service = get_services(request)
    usage_queue = get_usage_queue(request)

    # Authentication - prefer Authorization: Bearer, then x-api-key
    api_key = ""
    auth_header = request.headers.get("Authorization", "")

    if auth_header.startswith("Bearer "):
        _, _, api_key = auth_header.partition(" ")

    # If there is no Authorization header, try x-api-key
    if not api_key:
        api_key = request.headers.get("x-api-key", "")

    if not api_key:
        raise HTTPException(status_code=401, detail="Invalid API Key")

    # Validate the API key and usage limit
    await api_service.validate_api_key(api_key, session)
    await api_service.check_usage_limit(api_key, session)

    # Request handling
    req_data = await request.json()
    model = req_data.get("model")

    # Get the target server
    target_server = llm_service.get_target_server(model)

    # Get the model weights (for the Anthropic-style usage computation)
    input_weight, output_weight = await api_service._get_model_weights(model, session, target_server)

    # Log the request
    masked_key = f"{api_key[:8]}...{api_key[-4:]}"
    logger.info(f"coding request | model={model} | key={masked_key} | server={target_server}")

    # Build the target URL - strip the /coding prefix, keep the rest of the path
    original_path = request.url.path
    if original_path.startswith("/coding/"):
        path = original_path.replace("/coding", "", 1)
    else:
        path = "/chat/completions"
    target = f"{target_server}{path}"

    # Build the request headers - use the OpenAI-compatible format
    upstream_api_key = api_key
    if model in llm_service.app_state.cloud_models:
        server_mappings = llm_service.app_state.cloud_models[model]
        upstream_api_key = server_mappings.get(target_server, api_key)
    headers = {
        "Authorization": f"Bearer {upstream_api_key}",
        "Content-Type": "application/json",
    }

    try:
        # Streaming response handling
        if req_data.get("stream", False):
            async def stream_wrapper():
                max_retries = 1
                has_yielded = False  # whether content has been sent to the client (determines whether a retry is allowed after a disconnect)
                should_bill = False  # the upstream stream was established normally (2xx); billing is required on finish or disconnect
                server_key = llm_service._extract_server_key(target)

                try:
                    for attempt in range(max_retries + 1):
                        try:
                            client_stream = await llm_service.forward_request(
                                target, req_data, headers, stream=True
                            )

                            async with client_stream as response:
                                # Upstream returned an error status: record the circuit failure, pass through the error and stop (no billing, no counting)
                                if response.status_code >= 400:
                                    error_body = await response.aread()
                                    await llm_service.circuit_breaker.record_failure(server_key)
                                    llm_service._update_server_health(server_key, False)
                                    logger.warning(
                                        f"coding stream upstream error | model={model} | status={response.status_code}"
                                    )
                                    error_data = {
                                        "error": {
                                            "message": f"Upstream service returned an error (status code {response.status_code})",
                                            "type": "upstream_error",
                                            "code": response.status_code,
                                        }
                                    }
                                    yield f"data: {json.dumps(error_data)}\n\n"
                                    yield "data: [DONE]\n\n"
                                    return

                                should_bill = True
                                async for chunk in response.aiter_text():
                                    has_yielded = True
                                    yield chunk

                            # Streaming response completed successfully, record the success in the circuit breaker
                            await llm_service.circuit_breaker.record_success(server_key)
                            llm_service._update_server_health(server_key, True)
                            logger.info(f"coding stream completed | model={model} | key={masked_key}")
                            break  # completed successfully

                        except httpx.RemoteProtocolError as exc:
                            await llm_service.circuit_breaker.record_failure(server_key)
                            llm_service._update_server_health(server_key, False)
                            logger.warning(
                                f"Stream connection error (attempt {attempt + 1}/{max_retries + 1}) model={model}: {exc}"
                            )
                            # Only allow a retry when nothing has been sent to the client yet, to avoid duplicate pushes
                            if attempt < max_retries and not has_yielded:
                                await asyncio.sleep(0.5)
                                continue
                            error_data = {
                                "error": {
                                    "message": f"Upstream service connection interrupted: {str(exc)}",
                                    "type": "connection_error",
                                    "code": "connection_terminated"
                                }
                            }
                            yield f"data: {json.dumps(error_data)}\n\n"
                            yield "data: [DONE]\n\n"
                            break

                        except Exception as exc:
                            # HTTPException (e.g. circuit breaker open) is already recorded upstream; avoid double counting
                            if not isinstance(exc, HTTPException):
                                await llm_service.circuit_breaker.record_failure(server_key)
                                llm_service._update_server_health(server_key, False)
                            logger.error(f"Stream error model={model}: {exc}")
                            error_data = {
                                "error": {
                                    "message": f"Streaming response error: {str(exc)}",
                                    "type": "stream_error"
                                }
                            }
                            yield f"data: {json.dumps(error_data)}\n\n"
                            yield "data: [DONE]\n\n"
                            break
                finally:
                    # Bill as long as the upstream stream was established, whether it
                    # ended normally or the client disconnected
                    if should_bill:
                        try:
                            await usage_queue.enqueue(
                                UsageEventData(
                                    event_type=UsageEventType.UPDATE_ANTHROPIC_USAGE,
                                    api_key=api_key,
                                    model=model,
                                    server_url=target_server,
                                    input_token_weight=input_weight,
                                    output_token_weight=output_weight,
                                )
                            )
                            await usage_queue.enqueue(
                                UsageEventData(
                                    event_type=UsageEventType.INCREMENT_MODEL_REQS,
                                    api_key=api_key,
                                    model=model,
                                    server_url=target_server,
                                )
                            )
                        except Exception as e:
                            logger.warning(f"Failed to enqueue coding streaming billing model={model}: {e}")
                    else:
                        # Upstream error produced no billing, release the reserved concurrent quota
                        await api_service.release_in_flight(api_key)

            return StreamingResponse(
                stream_wrapper(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        # Normal response handling
        response_text = await llm_service.forward_request(target, req_data, headers)

        try:
            response = json.loads(response_text)

            # Normal response succeeded
            logger.info(f"coding response completed | model={model} | key={masked_key}")

            # Use the Anthropic-style usage statistics (by request count, no token counting)
            await usage_queue.enqueue(
                UsageEventData(
                    event_type=UsageEventType.UPDATE_ANTHROPIC_USAGE,
                    api_key=api_key,
                    model=model,
                    server_url=target_server,
                    input_token_weight=input_weight,
                    output_token_weight=output_weight,
                )
            )
            await usage_queue.enqueue(
                UsageEventData(
                    event_type=UsageEventType.INCREMENT_MODEL_REQS,
                    api_key=api_key,
                    model=model,
                    server_url=target_server,
                )
            )

            return JSONResponse(response)
        except json.JSONDecodeError as e:
            await api_service.release_in_flight(api_key)
            return JSONResponse(
                {"error": "Invalid response from upstream server", "message": str(e)},
                status_code=500,
            )

    except HTTPException as e:
        await api_service.release_in_flight(api_key)
        return JSONResponse({"error": str(e.detail)}, status_code=e.status_code)
    except Exception as e:
        await api_service.release_in_flight(api_key)
        logger.error(f"coding error: {e}", exc_info=True)
        return JSONResponse({"error": "Internal server error"}, status_code=500)