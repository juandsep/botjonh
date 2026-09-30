import httpx
import respx

from assistant.channels.telegram import API_BASE, Telegram, parse_update


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


@respx.mock
def test_send_animation() -> None:
    route = respx.post(f"{API_BASE}/bot1:x/sendAnimation").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    Telegram("1:x").send_animation("42", "gif1")
    assert route.calls.last.request.read() == b'{"chat_id":"42","animation":"gif1"}'


def test_parse_animation_caption_and_reply() -> None:
    gif = {"update_id": 1, "message": {"chat": {"id": 42}, "caption": "gasto"}}
    gif["message"]["animation"] = {"file_id": "g1"}
    msg = parse_update(gif)
    assert msg and (msg.text, msg.caption, msg.animation_file_id) == ("", "gasto", "g1")
    reply = {
        "update_id": 2,
        "message": {
            "chat": {"id": 42},
            "text": "/gif ingreso",
            "reply_to_message": {"animation": {"file_id": "g2"}},
        },
    }
    msg = parse_update(reply)
    assert msg and msg.reply_animation_file_id == "g2" and msg.animation_file_id is None
    assert (
        parse_update({"update_id": 3, "message": {"chat": {"id": 1}, "animation": 5}})
        is None
    )
