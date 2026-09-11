"""Intercept only explicit /graph turns and replies to a pending intake."""
import asyncio

from .intake import Intake


def run_conversation(delegate, agent, user_message, system_message=None, conversation_history=None,
                     task_id=None, stream_callback=None, persist_user_message=None, **kwargs):
    if not isinstance(user_message, str) or not getattr(agent, "session_id", None):
        return delegate(agent, user_message, system_message, conversation_history, task_id,
                        stream_callback, persist_user_message, **kwargs)
    intake = Intake(agent.session_id)
    explicit = user_message == "/graph" or user_message.startswith("/graph ")
    draft = intake.load()
    active = draft and draft["status"] in {"collecting", "ready", "running"}
    if not explicit and (not active or user_message.startswith("/")):
        return delegate(agent, user_message, system_message, conversation_history, task_id,
                        stream_callback, persist_user_message, **kwargs)
    from agent.runtime_cwd import scope_terminal_cwd
    async def respond():
        task = asyncio.create_task(intake.handle(user_message, cwd=scope_terminal_cwd() or None,
                                                progress=stream_callback,
                                                interrupted=lambda: bool(getattr(agent, "_interrupt_requested", False))))
        try:
            while not task.done():
                await asyncio.wait({task}, timeout=0.25)
                if getattr(agent, "_interrupt_requested", False) and not task.cancelling():
                    task.cancel()
            return await task
        except asyncio.CancelledError:
            return "Graph-Dialog gestoppt. Der gespeicherte Stand ist mit /graph status abrufbar."
    try:
        reply = asyncio.run(respond())
    except Exception as exc:
        reply = f"Graph-Dialog konnte nicht fortgesetzt werden ({type(exc).__name__}). Bitte /graph status prüfen."
    history = list(conversation_history or [])
    # Admission may already carry the current persisted user row (replay/retry).
    if not history or history[-1].get("role") != "user":
        row = {"role": "user", "content": persist_user_message or user_message}
        for source, target in {"persist_user_timestamp": "timestamp", "persist_user_display_kind": "display_kind",
                               "persist_user_display_metadata": "display_metadata", "persist_user_platform_id": "platform_message_id"}.items():
            if kwargs.get(source) is not None:
                row[target] = kwargs[source]
        history.append(row)
    history.append({"role": "assistant", "content": reply})
    agent._flush_messages_to_session_db(history, conversation_history)
    if stream_callback:
        stream_callback(reply)
    interrupted = bool(getattr(agent, "_interrupt_requested", False))
    if interrupted and callable(getattr(agent, "clear_interrupt", None)):
        agent.clear_interrupt()
    return {"final_response": reply, "messages": history, "completed": True, "failed": False,
            "interrupted": interrupted, "api_calls": 0,
            "session_id": agent.session_id, "turn_exit_reason": "graph_dialogue"}
