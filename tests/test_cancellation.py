from __future__ import annotations

from diskscope.cancellation import CancelToken


def test_cancel_token_is_one_way() -> None:
    token = CancelToken()
    assert token.is_cancelled is False
    token.cancel()
    assert token.is_cancelled is True
    token.cancel()
    assert token.is_cancelled is True


def test_cancel_token_can_be_shared_between_threads() -> None:
    import threading

    token = CancelToken()
    worker = threading.Thread(target=token.cancel)
    worker.start()
    worker.join()
    assert token.is_cancelled is True
