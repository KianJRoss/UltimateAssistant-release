"""Verified Antigravity 1.3.1 print-mode input, independent of argv size."""
import json


def antigravity_stream_input(executable, arguments, prompt, output_schema=None):
    """Keep the complete prompt in one native user-message NDJSON record."""
    if not isinstance(prompt,str):
        raise TypeError('Native prompt must be text')
    valued={'--input-format','--output-format','--print','--prompt','-p'}
    schema_args=[]
    if output_schema is not None:
        if not isinstance(output_schema,dict):
            raise TypeError('Native output schema must be an object')
        schema=json.dumps(output_schema,ensure_ascii=False,allow_nan=False,separators=(',',':'))
        if len(schema.encode('utf-8'))>65536:
            raise ValueError('Native output schema exceeds bounded argument size')
        valued.add('--json-schema')
        schema_args=['--json-schema',schema]
    clean=[]
    skip=False
    for argument in arguments:
        if skip:
            skip=False
            continue
        if argument in valued:
            skip=True
            continue
        if any(argument.startswith(flag+'=') for flag in valued):
            continue
        clean.append(argument)
    cmd=[*executable,*clean,*schema_args,'--input-format','stream-json','--output-format','stream-json','--print','']
    message={'event':'user','message':{'role':'user','content':[{'type':'text','text':prompt}]}}
    return cmd,json.dumps(message,ensure_ascii=False,separators=(',',':'))+'\n'
