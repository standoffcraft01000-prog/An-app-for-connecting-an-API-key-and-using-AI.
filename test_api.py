"""Offline tests for GikAI API request formatting. No real API key or network needed."""
import unittest
from unittest.mock import patch

import main


class ApiFormattingTests(unittest.TestCase):
    def test_openai_compatible_request(self):
        settings = {
            "provider": "OpenAI",
            "base_url": "https://example.test/v1/",
            "model": "demo-model",
            "api_key": "TEST_KEY_DO_NOT_USE",
        }
        history = [("user", "Привет"), ("assistant", "Здравствуйте"), ("user", "Как дела?")]
        fake_response = {"choices": [{"message": {"content": "Всё хорошо!"}}]}
        with patch.object(main, "_post_json", return_value=fake_response) as post:
            answer = main._openai_compatible_request(settings, history)
        self.assertEqual(answer, "Всё хорошо!")
        args, kwargs = post.call_args
        self.assertEqual(args[0], "https://example.test/v1/chat/completions")
        self.assertEqual(args[1]["Authorization"], "Bearer TEST_KEY_DO_NOT_USE")
        self.assertEqual(args[2]["model"], "demo-model")
        self.assertEqual(len(args[2]["messages"]), 3)

    def test_gemini_request(self):
        settings = {
            "provider": "Google Gemini",
            "base_url": "https://generativelanguage.googleapis.com/v1beta",
            "model": "demo-model",
            "api_key": "TEST_KEY_DO_NOT_USE",
        }
        history = [("user", "Привет"), ("assistant", "Здравствуйте")]
        fake_response = {"candidates": [{"content": {"parts": [{"text": "Ответ Gemini"}]}}]}
        with patch.object(main, "_post_json", return_value=fake_response) as post:
            answer = main._gemini_request(settings, history)
        self.assertEqual(answer, "Ответ Gemini")
        args, kwargs = post.call_args
        self.assertEqual(args[0], "https://generativelanguage.googleapis.com/v1beta/models/demo-model:generateContent")
        self.assertEqual(args[1]["x-goog-api-key"], "TEST_KEY_DO_NOT_USE")
        self.assertEqual(args[2]["contents"][1]["role"], "model")
        self.assertEqual(args[2]["contents"][0]["parts"][0]["text"], "Привет")

    def test_missing_response_is_clear_error(self):
        settings = {
            "provider": "OpenAI",
            "base_url": "https://example.test/v1",
            "model": "demo-model",
            "api_key": "TEST_KEY_DO_NOT_USE",
        }
        with patch.object(main, "_post_json", return_value={"choices": []}):
            with self.assertRaises(main.APIRequestError):
                main._openai_compatible_request(settings, [("user", "Привет")])


class MessageRenderingTests(unittest.TestCase):
    def test_render_message_uses_pack_padding_not_label_padding_tuple(self):
        class FakeWidget:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.pack_kwargs = None
                self.bg = kwargs.get("bg")

            def __getitem__(self, key):
                return self.kwargs[key]

            def pack(self, **kwargs):
                self.pack_kwargs = kwargs

        created_labels = []

        def fake_label(_parent, **kwargs):
            widget = FakeWidget(**kwargs)
            created_labels.append(widget)
            return widget

        app = main.GikAIApp.__new__(main.GikAIApp)
        app.chat_inner = FakeWidget(bg=main.BG)
        with patch.object(main.tk, "Frame", side_effect=lambda *_a, **kw: FakeWidget(**kw)), \
             patch.object(main.tk, "Label", side_effect=fake_label):
            app._render_message("user", "Тестовое сообщение")

        message_label = created_labels[1]
        self.assertEqual(message_label.kwargs["pady"], 0)
        self.assertEqual(message_label.pack_kwargs["pady"], (0, 11))


if __name__ == "__main__":
    unittest.main(verbosity=2)
