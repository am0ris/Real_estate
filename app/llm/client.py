from typing import TypeVar
from app.llm.prompts import SYSTEM_PROMPT
from groq import Groq
from pydantic import BaseModel
from app.helpers.config import get_settings
from app.llm.params import reasoning_params


T = TypeVar("T", bound=BaseModel)


class LLMClient:
    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
    ):
        settings = get_settings()
        configured_api_key = api_key or settings.GROQ_API_KEY
        if not configured_api_key:
            raise RuntimeError("GROQ_API_KEY is not configured.")
        self.client = Groq(api_key=configured_api_key)
        self.model = model or settings.GENERATION_MODEL or settings.GROQ_MODEL

    def generate(
        self,
        prompt: str,
        system_prompt: str = SYSTEM_PROMPT,
    ) -> str:
        messages = []

        if system_prompt:
            messages.append({
                "role": "system",
                "content": system_prompt,
            })

        messages.append({
            "role": "user",
            "content": prompt,
        })

        return self.generate_messages(messages)

    def generate_messages(
        self,
        messages: list[dict[str, str]],
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """Generate from an explicit conversation message list."""
        if not messages:
            raise ValueError("At least one message is required for generation.")

        request = {
            "model": self.model,
            "messages": messages,
            **reasoning_params(self.model),
        }
        if temperature is not None:
            request["temperature"] = temperature
        if max_tokens is not None:
            request["max_tokens"] = max_tokens

        response = self.client.chat.completions.create(**request)
        if not response.choices:
            raise ValueError("LLM returned no completion choices.")
        return response.choices[0].message.content or ""

    def generate_structured(
        self,
        prompt: str,
        output_schema: type[T],
        system_prompt: str = SYSTEM_PROMPT,
    ) -> T:
        messages = []

        if system_prompt:
            messages.append({
                "role": "system",
                "content": system_prompt,
            })

        messages.append({
            "role": "user",
            "content": prompt,
        })

        response = self.client.chat.completions.parse(
            model=self.model,
            messages=messages,
            response_format=output_schema,
        )

        parsed = response.choices[0].message.parsed

        if parsed is None:
            raise ValueError(
                "LLM returned no structured output."
            )

        return parsed
