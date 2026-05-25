from .base import load_prompt, run_claude


async def run_po(task: str, context: str = "") -> str:
    system = load_prompt("po")
    user = task if not context else f"{task}\n\n---\nThread context:\n{context}"
    return await run_claude(system, user)
