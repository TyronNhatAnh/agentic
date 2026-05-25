from .base import load_prompt, run_claude


async def run_ba(task: str, context: str = "") -> str:
    system = load_prompt("ba")
    user = task if not context else f"{task}\n\n---\nThread context:\n{context}"
    return await run_claude(system, user, prompt_mode="append")
