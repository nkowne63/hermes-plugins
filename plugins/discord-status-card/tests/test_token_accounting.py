import asyncio
import importlib.util
from pathlib import Path


def test_latest_context_can_shrink_without_resetting_turn_usage():
    spec = importlib.util.spec_from_file_location("card_token_test", Path(__file__).parents[1] / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    loop = asyncio.new_event_loop()
    try:
        card = module._Card(adapter=None, loop=loop, keys=(), dest="1", requester="1", metadata={})
        card.poke_threadsafe = lambda: None
        assert card.context_tokens is None
        card.on_api({"prompt_tokens": 30000, "input_tokens": 1000, "output_tokens": 100}, None)
        card.on_api({"prompt_tokens": 32000, "input_tokens": 2000, "output_tokens": 200}, None)
        assert card.context_tokens == 32000
        assert (card.tokens_in, card.tokens_out) == (62000, 300)
        card.on_api(None, "reasoning without usage")
        assert card.context_tokens == 32000
        card.on_api({"input_tokens": 8000, "output_tokens": 50}, None)
        assert card.context_tokens == 8000
        assert (card.tokens_in, card.tokens_out) == (70000, 350)
        card.on_api({"output_tokens": 10}, None)
        assert card.context_tokens == 8000
        card.on_api({"prompt_tokens": 0, "input_tokens": 999}, None)
        assert card.context_tokens == 0
        assert card.tokens_in == 70000
    finally:
        loop.close()
