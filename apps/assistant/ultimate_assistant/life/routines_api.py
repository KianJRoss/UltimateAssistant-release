"""Authenticated private routine interfaces using the shared durable store."""
import sqlite3
from datetime import datetime,timezone
from fastapi import Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from .routine_store import RoutineConflict, RoutineStore

class StrictBody(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)

class CreateRoutine(StrictBody):
    title: str=Field(min_length=1,max_length=200)
    interval_days: int=Field(ge=1,le=3660)
    estimated_minutes: float=Field(gt=0,le=1440,allow_inf_nan=False)
    next_due_at: str=Field(min_length=1)
    notes: str=Field(default='',max_length=4000)
    request_id: str=Field(min_length=1,max_length=256)

class CompleteRoutine(StrictBody):
    revision: int=Field(ge=1)
    request_id: str=Field(min_length=1,max_length=256)
    completed_at: str|None=None

class ActiveRoutine(StrictBody):
    revision: int=Field(ge=1)
    request_id: str=Field(min_length=1,max_length=256)
    active: bool

async def body(request,model):
    try:return model.model_validate(await request.json())
    except (ValueError,TypeError,ValidationError) as exc:
        raise HTTPException(400,'Invalid routine request') from exc

def mutate(action):
    try:return action()
    except RoutineConflict as exc:raise HTTPException(409,str(exc)) from exc
    except KeyError as exc:raise HTTPException(404,'Routine not found') from exc
    except ValueError as exc:raise HTTPException(400,str(exc)) from exc
    except (sqlite3.Error,OSError,RuntimeError) as exc:
        raise HTTPException(503,'Routine storage is unavailable') from exc

def register(app,shared,headers):
    auth=[Depends(shared._require_app_token)]
    @app.get('/api/routines',dependencies=auth)
    def routines_status():
        try:return RoutineStore().snapshot()
        except (OSError,RuntimeError,sqlite3.Error):
            return {'state':'unavailable','as_of':datetime.now(timezone.utc).isoformat(),'items':[],'total':0,'omitted':0}

    @app.post('/api/routines',dependencies=auth)
    async def create_routine(request:Request):
        value=await body(request,CreateRoutine)
        return await run_in_threadpool(mutate,lambda:RoutineStore().create(**value.model_dump()))

    @app.post('/api/routines/{routine_id}/complete',dependencies=auth)
    async def complete_routine(routine_id:str,request:Request):
        value=await body(request,CompleteRoutine)
        return await run_in_threadpool(mutate,lambda:RoutineStore().complete(routine_id,**value.model_dump()))

    @app.post('/api/routines/{routine_id}/active',dependencies=auth)
    async def set_routine_active(routine_id:str,request:Request):
        value=await body(request,ActiveRoutine)
        return await run_in_threadpool(mutate,lambda:RoutineStore().set_active(routine_id,**value.model_dump()))
