import httpx
import respx

from assistant.channels.telegram import API_BASE, Telegram


@respx.mock
def test_send_message_with_keyboard() -> None:
    route = respx.post(f"{API_BASE}/bot1:x/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    Telegram("1:x").send_message("42", "hola", [[("Sí", "ok:1")]])
    body = route.calls.last.request.read()
    assert b'"callback_data":"ok:1"' in body


@respx.mock
def test_answer_callback() -> None:
    route = respx.post(f"{API_BASE}/bot1:x/answerCallbackQuery").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    Telegram("1:x").answer_callback("cb", "✓")
    assert route.called
