"""Persist provider-prefetched evidence using ordinary tool protocol semantics."""
import json
import logging
from uuid import uuid4

from agent.memory_manager import memory_provider_tools_exposed

logger=logging.getLogger(__name__)


def append_memory_tool_context(agent, messages, query):
    manager=getattr(agent,'_memory_manager',None)
    if manager is None or not messages or messages[-1].get('role')!='user':
        return 0
    if not memory_provider_tools_exposed(agent):
        return 0
    exposed={tool.get('function',{}).get('name') for tool in (agent.tools or [])}
    appended=0
    for provider in manager.providers:
        consume=getattr(provider,'take_tool_context',None)
        if not callable(consume):
            continue
        try:
            result=consume(query,session_id=agent.session_id or '')
            if not result:
                continue
            name=result.get('name')
            content=result.get('content')
            arguments=result.get('arguments',{})
            if name not in exposed or not isinstance(content,str) or not content or not isinstance(arguments,dict):
                raise ValueError('invalid or unexposed memory tool context')
            if len(content.encode('utf-8'))>32000:
                raise ValueError('automatic memory tool context exceeds host bound')
            call_id='call_'+uuid4().hex[:24]
            messages.extend([
                {'role':'assistant','content':None,'tool_calls':[{'id':call_id,'type':'function',
                    'function':{'name':name,'arguments':json.dumps(arguments,ensure_ascii=False)}}]},
                {'role':'tool','tool_call_id':call_id,'name':name,'content':content},
            ])
            appended+=1
        except Exception:
            logger.warning('Automatic memory tool context rejected for %s',provider.name,exc_info=True)
    return appended
