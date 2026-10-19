"""MCP schemas for optional, scope-bound native assistant jobs."""
from mcp import types

NAMES = {'herald_start_assistant_work', 'herald_inspect_assistant_work', 'herald_list_assistant_work', 'herald_cancel_assistant_work'}

def tools():
    return [
        types.Tool(name='herald_start_assistant_work', description='Start optional background work through a configured full native CLI. Use agent for one delegate, swarm for 1-4 independent tasks, or loop for up to 6 iterations with an explicit completion marker. Supply relevant context and current activity_id. Nested jobs must supply their current parent_id. Inspect results before claiming completion. No recurring schedule is created.', inputSchema={'type':'object','properties':{
            'request_id':{'type':'string','minLength':1,'maxLength':100,'description':'Unique task key; reuse it for retries of this exact request and use a new key only for deliberate new work'},
            'model':{'type':'string','description':'An enabled native CLI backend name from the connected Router'},
            'prompt':{'type':'string','minLength':1,'maxLength':12000},
            'kind':{'type':'string','enum':['agent','swarm','loop'],'default':'agent'},
            'parent_id':{'type':'string'},'activity_id':{'type':'string','maxLength':100},
            'context':{'type':'string','maxLength':16000,'description':'Relevant user instructions, verified findings, task state and memory provenance; exclude credentials and unrelated personal data'},
            'tasks':{'type':'array','minItems':1,'maxItems':4,'items':{'type':'string','maxLength':12000}},
            'max_iterations':{'type':'integer','minimum':1,'maximum':6,'default':3},
            'stop_marker':{'type':'string','minLength':1,'maxLength':80,'default':'TASK_COMPLETE'}},'required':['request_id','model','prompt'],'additionalProperties':False}),
        types.Tool(name='herald_list_assistant_work',description='List saved delegated jobs in this assistant scope, including parent/child relationships and failures. Optional activity_id limits results to one user turn.',inputSchema={'type':'object','properties':{'activity_id':{'type':'string'}},'additionalProperties':False}),
        types.Tool(name='herald_inspect_assistant_work',description='Inspect a delegated job, saved result, progress and errors in this assistant scope. Reuse the saved result after reconnecting; do not repeat a job with side effects.',inputSchema={'type':'object','properties':{'job_id':{'type':'string'}},'required':['job_id'],'additionalProperties':False}),
        types.Tool(name='herald_cancel_assistant_work',description='Request cancellation of a delegated job and its descendants. Inspect afterward to confirm native execution stopped; cancellation does not undo completed actions.',inputSchema={'type':'object','properties':{'job_id':{'type':'string'}},'required':['job_id'],'additionalProperties':False}),
    ]
