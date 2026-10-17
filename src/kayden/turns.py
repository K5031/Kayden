from kayden.events import TurnRequest


def build_turn(
    system_prompt: str,
    context: list[dict],
    memories: str,
    user_input: str,
) -> TurnRequest:
    system_content = system_prompt
    if memories:
        system_content += f"\n\nRelevant memories about the user:\n{memories}"
    return TurnRequest(messages=[
        {"role": "system", "content": system_content},
        *context,
        {"role": "user", "content": user_input},
    ])
