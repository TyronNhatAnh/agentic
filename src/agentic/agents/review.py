from .base import load_prompt, run_claude


async def run_review(task: str, context: str = "") -> str:
    system = load_prompt("review")
    user = task if not context else f"{task}\n\n---\nDiff / context:\n{context}"
    return await run_claude(system, user)
