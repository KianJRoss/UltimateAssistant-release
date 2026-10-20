"""The life layer: what the user has to do and when, and the assistant staying ahead of it.

Stores: life_inbox (chores, appointments, outings, hobbies), routine_store, task_units, availability,
planning_state, progress_state, work_sessions, preparation (proactive records, standing authority).
Policy: deadline_policy, commitment_engine, commitment_orchestration, planning_service, today.
Delivery: proactive_service, proactive_delivery. Storage root: paths.life_root().
"""
