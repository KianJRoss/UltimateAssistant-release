"""Authenticated private life inbox interfaces using the shared durable store."""
from pathlib import Path
import sqlite3
from datetime import datetime, timezone
from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from .life_inbox import LifeConflict, LifeInbox
from .routine_store import RoutineConflict

class StrictBody(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

class CaptureAppointment(StrictBody):
    title: str = Field(min_length=1, max_length=500)
    starts_at: str = Field(min_length=1)
    ends_at: str | None = None
    location: str = Field(default='', max_length=2000)
    notes: str = Field(default='', max_length=8000)
    request_id: str = Field(min_length=1, max_length=256)

class CaptureAwayWindow(StrictBody):
    destination: str = Field(default='', max_length=2000)
    starts_at: str = Field(min_length=1)
    expected_return: str | None = None
    notes: str = Field(default='', max_length=8000)
    request_id: str = Field(min_length=1, max_length=256)

class RecordReturn(StrictBody):
    revision: int = Field(ge=1)
    request_id: str = Field(min_length=1, max_length=256)
    returned_at: str | None = None

class CaptureHobbyAction(StrictBody):
    hobby_name: str = Field(min_length=1, max_length=200)
    next_action: str = Field(min_length=1, max_length=2000)
    target_date: str | None = None
    notes: str = Field(default='', max_length=8000)
    request_id: str = Field(min_length=1, max_length=256)

class UpdateItemStatus(StrictBody):
    status: str = Field(min_length=1, max_length=64)
    revision: int = Field(ge=1)
    request_id: str = Field(min_length=1, max_length=256)

class CaptureChore(StrictBody):
    title: str = Field(min_length=1, max_length=200)
    interval_days: int = Field(ge=1, le=3660)
    estimated_minutes: float = Field(gt=0, le=1440, allow_inf_nan=False)
    next_due_at: str = Field(min_length=1)
    notes: str = Field(default='', max_length=4000)
    request_id: str = Field(min_length=1, max_length=256)

class CompleteChore(StrictBody):
    revision: int = Field(ge=1)
    request_id: str = Field(min_length=1, max_length=256)
    completed_at: str | None = None

class DefineEstimates(StrictBody):
    activity_id: str = Field(min_length=1, max_length=512)
    units: list[dict] = Field(min_length=1, max_length=500)
    request_id: str = Field(min_length=1, max_length=512)
    expected_revision: int | None = Field(default=None, ge=0)
    provenance: str = Field(default='', max_length=16384)

async def body(request: Request, model):
    try:
        return model.model_validate(await request.json())
    except (ValueError, TypeError, ValidationError) as exc:
        raise HTTPException(400, 'Invalid life request') from exc

def mutate(action):
    try:
        return action()
    except (LifeConflict, RoutineConflict) as exc:
        raise HTTPException(409, str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(404, 'Life item not found') from exc
    except ValueError as exc:
        detail = str(exc)
        if 'expected_revision does not match current revision' in detail or 'Conflict' in detail:
            raise HTTPException(409, detail) from exc
        raise HTTPException(400, detail) from exc
    except (sqlite3.Error, OSError, RuntimeError) as exc:
        raise HTTPException(503, 'Life storage is unavailable') from exc

def register(app, shared, headers):
    auth = [Depends(shared._require_app_token)]

    @app.get('/api/life', dependencies=auth)
    def life_status(kind: str | None = None, limit: int = 100):
        try:
            return LifeInbox().snapshot(kind=kind, limit=limit)
        except (OSError, RuntimeError, sqlite3.Error):
            return {
                'state': 'unavailable',
                'as_of': datetime.now(timezone.utc).isoformat(),
                'items': [],
                'total_items': 0,
                'chores': {'state': 'unavailable', 'as_of': datetime.now(timezone.utc).isoformat(), 'items': [], 'total': 0, 'omitted': 0},
                'task_units': {'state': 'unavailable', 'records': [], 'total': 0},
            }

    @app.post('/api/life/appointments', dependencies=auth)
    async def create_appointment(request: Request):
        value = await body(request, CaptureAppointment)
        return await run_in_threadpool(mutate, lambda: LifeInbox().capture_appointment(**value.model_dump()))

    @app.post('/api/life/away', dependencies=auth)
    async def create_away_window(request: Request):
        value = await body(request, CaptureAwayWindow)
        return await run_in_threadpool(mutate, lambda: LifeInbox().capture_away_window(**value.model_dump()))

    @app.post('/api/life/away/{item_id}/return', dependencies=auth)
    async def record_return(item_id: str, request: Request):
        value = await body(request, RecordReturn)
        return await run_in_threadpool(mutate, lambda: LifeInbox().record_return(item_id, **value.model_dump()))

    @app.post('/api/life/hobbies', dependencies=auth)
    async def create_hobby_action(request: Request):
        value = await body(request, CaptureHobbyAction)
        return await run_in_threadpool(mutate, lambda: LifeInbox().capture_hobby_action(**value.model_dump()))

    @app.post('/api/life/items/{item_id}/status', dependencies=auth)
    async def update_item_status(item_id: str, request: Request):
        value = await body(request, UpdateItemStatus)
        return await run_in_threadpool(mutate, lambda: LifeInbox().update_item_status(item_id, **value.model_dump()))

    @app.post('/api/life/chores', dependencies=auth)
    async def create_chore(request: Request):
        value = await body(request, CaptureChore)
        return await run_in_threadpool(mutate, lambda: LifeInbox().capture_chore(**value.model_dump()))

    @app.post('/api/life/chores/{routine_id}/complete', dependencies=auth)
    async def complete_chore(routine_id: str, request: Request):
        value = await body(request, CompleteChore)
        return await run_in_threadpool(mutate, lambda: LifeInbox().complete_chore(routine_id, **value.model_dump()))

    @app.post('/api/life/estimates', dependencies=auth)
    async def define_estimates(request: Request):
        value = await body(request, DefineEstimates)
        return await run_in_threadpool(mutate, lambda: LifeInbox().define_estimates(**value.model_dump()))
