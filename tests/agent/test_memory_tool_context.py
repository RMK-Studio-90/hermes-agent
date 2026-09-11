"""Transport contracts, not proof of any backend's memory quality."""
from types import SimpleNamespace

from agent.memory_tool_context import append_memory_tool_context


def test_automatic_memory_uses_paired_tool_protocol_without_rewriting_history():
    class Provider:
        name='transport-fixture'

        def take_tool_context(self,query,*,session_id=''):
            return {'name':'memory_lookup','arguments':{'query':query},'content':'Evidence payload'}

    history=[{'role':'user','content':'Earlier question'},{'role':'assistant','content':'Earlier answer'}]
    messages=[*history,{'role':'user','content':'Current question'}]
    agent=SimpleNamespace(_memory_manager=SimpleNamespace(providers=[Provider()]),session_id='s',
        enabled_toolsets=['memory'],disabled_toolsets=[],tools=[{'type':'function','function':{'name':'memory_lookup'}}])
    assert append_memory_tool_context(agent,messages,'Current question')==1
    assert messages[:2]==history
    assert messages[2]=={'role':'user','content':'Current question'}
    call,result=messages[-2:]
    assert call['role']=='assistant' and result['role']=='tool'
    assert call['tool_calls'][0]['id']==result['tool_call_id']
    assert call['tool_calls'][0]['function']['name']==result['name']
    assert append_memory_tool_context(agent,messages,'Current question')==0


def test_disabled_memory_does_not_consume_prefetched_evidence():
    class Provider:
        name='transport-fixture'

        def take_tool_context(self,*args,**kwargs):
            raise AssertionError('Disabled memory must not be consumed or counted')

    messages=[{'role':'user','content':'Question'}]
    agent=SimpleNamespace(_memory_manager=SimpleNamespace(providers=[Provider()]),session_id='s',
        enabled_toolsets=['memory'],disabled_toolsets=['memory'],tools=[])
    assert append_memory_tool_context(agent,messages,'Question')==0
    assert len(messages)==1
