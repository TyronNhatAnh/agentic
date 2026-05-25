from .base import load_prompt, run_claude


async def run_dev(task: str, context: str = "", cwd: str | None = None) -> str:
    system = load_prompt("dev")
    user = task if not context else f"{task}\n\n---\nContext:\n{context}"
    return await run_claude(system, user, cwd=cwd)
