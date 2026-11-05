"""Device voice interface offered through the normal Herald node connection."""
import json
import socket
from mcp.server.fastmcp import FastMCP
from ultimate_assistant.desktop import socket_path

mcp=FastMCP('spoken-conversation')

def command(action,text=''):
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(5);client.connect(str(socket_path()))
        client.sendall((json.dumps({'action':action,'text':text})+'\n').encode())
        result=json.loads(client.makefile().readline())
    if result.get('error'):raise RuntimeError(result['error'])
    return result

@mcp.tool()
def voice_status() -> dict:
    """Whether this device can speak/listen and which conversation receives the answer."""
    return command('status')

@mcp.tool()
def speak_to_user(text:str,delivery_key:str,listen:bool=True,reading_session:str='') -> dict:
    """Speak on this device and optionally listen for a bounded reply in the same conversation.
    Use a stable unique key per utterance to avoid retries repeating it. Accepted is not proof
    of audible completion: check spoken_delivery_status. Keep questions short; reading passages
    may be longer. Never claim the user heard or answered until the corresponding evidence exists.
    """
    return command('announce',json.dumps({'text':text,'id':delivery_key,'listen':listen,'reading_session':reading_session}))

@mcp.tool()
def spoken_delivery_status(delivery_key:str) -> dict:
    """Accepted/speaking/completed/interrupted/failed evidence for a spoken turn."""
    return command('delivery_status',delivery_key)

@mcp.tool()
def stop_speaking() -> dict:
    """Stop this device's speech and end its listening session when the user asks."""
    return command('cancel')

if __name__=='__main__':mcp.run()
