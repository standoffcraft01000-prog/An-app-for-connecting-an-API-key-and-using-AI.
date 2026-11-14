from __future__ import annotations

import json
import os
import queue
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox

try:
    import keyring
except Exception:
    keyring = None

APP_NAME = "GikAI"
SERVICE_NAME = "GikAI Desktop"

# Palette
BG = "#0B1114"
SIDEBAR = "#10191D"
PANEL = "#121E23"
CARD = "#17262B"
CARD_ALT = "#1B2C31"
BORDER = "#263A40"
TEXT = "#F0F7F4"
MUTED = "#91A69F"
GREEN = "#35D39A"
GREEN_DARK = "#1E6D51"
RED = "#F08B8B"


def app_data_dir() -> Path:
    if os.name == "nt":
        base = Path(os.getenv("APPDATA", Path.home()))
    else:
        base = Path(os.getenv("XDG_CONFIG_HOME", Path.home() / ".config"))
    folder = base / APP_NAME
    folder.mkdir(parents=True, exist_ok=True)
    return folder


CONFIG_PATH = app_data_dir() / "settings.json"


def load_config() -> dict:
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(data: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = CONFIG_PATH.with_suffix(".tmp")
    temp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(CONFIG_PATH)


class APIRequestError(Exception):
    """A safe, user-readable API request error."""


def _extract_api_error(raw_body: bytes) -> str:
    try:
        data = json.loads(raw_body.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return "Сервер вернул ошибку. Проверь адрес API и настройки провайдера."
    if isinstance(data, dict):
        error = data.get("error", data)
        if isinstance(error, dict):
            message = error.get("message") or error.get("status") or error.get("code")
            if message:
                return str(message)[:700]
        if isinstance(error, str):
            return error[:700]
    return "Сервер вернул ошибку без описания."


def _post_json(url: str, headers: dict[str, str], payload: dict) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        details = _extract_api_error(exc.read())
        raise APIRequestError(f"Ошибка API (HTTP {exc.code}): {details}") from None
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", None)
        if isinstance(reason, TimeoutError):
            raise APIRequestError("Сервер долго не отвечает. Попробуй ещё раз.") from None
        raise APIRequestError("Не удалось подключиться к серверу. Проверь интернет и Base URL.") from None
    except TimeoutError:
        raise APIRequestError("Сервер долго не отвечает. Попробуй ещё раз.") from None
    except OSError:
        raise APIRequestError("Сетевая ошибка. Проверь подключение к интернету.") from None

    try:
        data = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise APIRequestError("Сервер вернул ответ в неизвестном формате.") from None
    if not isinstance(data, dict):
        raise APIRequestError("Сервер вернул неожиданный формат ответа.")
    return data


def _openai_compatible_request(settings: dict, history: list[tuple[str, str]]) -> str:
    base_url = settings["base_url"].strip().rstrip("/")
    if base_url.endswith("/chat/completions"):
        endpoint = base_url
    else:
        endpoint = base_url + "/chat/completions"

    messages = [{"role": role, "content": text} for role, text in history]
    payload = {"model": settings["model"], "messages": messages}
    data = _post_json(endpoint, {
        "Authorization": f"Bearer {settings['api_key']}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "GikAI-Desktop/0.3.1",
    }, payload)

    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise APIRequestError("В ответе API нет choices. Проверь выбранную модель и совместимость API.")
    message = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str) and content.strip():
        return content.strip()
    if isinstance(content, list):
        parts = [item.get("text", "") for item in content if isinstance(item, dict)]
        result = "\n".join(part for part in parts if part).strip()
        if result:
            return result
    raise APIRequestError("Модель вернула пустой ответ. Проверь параметры модели в настройках.")


def _gemini_request(settings: dict, history: list[tuple[str, str]]) -> str:
    """Call Gemini's broadly supported generateContent REST endpoint."""
    base_url = (settings.get("base_url") or "https://generativelanguage.googleapis.com/v1beta").strip().rstrip("/")
    model = settings["model"].strip()
    if model.startswith("models/"):
        model = model[len("models/"):]
    if not model:
        raise APIRequestError("Не указана модель Gemini в настройках.")

    endpoint = f"{base_url}/models/{urllib.parse.quote(model, safe='')}:generateContent"
    contents = []
    for role, text in history:
        contents.append({
            "role": "model" if role == "assistant" else "user",
            "parts": [{"text": text}],
        })

    data = _post_json(endpoint, {
        "x-goog-api-key": settings["api_key"],
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "GikAI-Desktop/0.3.1",
    }, {"contents": contents})

    candidates = data.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        feedback = data.get("promptFeedback", {})
        reason = feedback.get("blockReason") if isinstance(feedback, dict) else None
        if reason:
            raise APIRequestError(f"Gemini заблокировал запрос: {reason}. Попробуй изменить сообщение.")
        raise APIRequestError("Gemini не вернул candidates. Проверь ID модели и доступ к ней для своего API-ключа.")

    candidate = candidates[0]
    if not isinstance(candidate, dict):
        raise APIRequestError("Gemini вернул ответ в неожиданном формате.")
    content = candidate.get("content", {})
    parts = content.get("parts", []) if isinstance(content, dict) else []
    answer = "\n".join(
        part.get("text", "") for part in parts
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    ).strip()
    if answer:
        return answer

    finish_reason = candidate.get("finishReason")
    if finish_reason:
        raise APIRequestError(f"Gemini не сформировал текстовый ответ. Причина: {finish_reason}.")
    raise APIRequestError("Gemini вернул пустой ответ. Проверь выбранную модель и настройки API.")


def request_model(settings: dict, history: list[tuple[str, str]]) -> str:
    provider = settings.get("provider", "Совместимый API")
    if provider == "Google Gemini":
        return _gemini_request(settings, history)
    return _openai_compatible_request(settings, history)


class GikAIApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("GikAI — личный ИИ")
        self.root.geometry("1180x760")
        self.root.minsize(960, 640)
        self.root.configure(bg=BG)

        self.config = load_config()
        self.current_page = "chat"
        self.api_key_saved = self._has_saved_key()
        self.chat_messages: list[tuple[str, str]] = []
        self.chat_session_id = 0
        self.request_queue: queue.Queue = queue.Queue()
        self.request_in_progress = False
        self.test_in_progress = False

        self._setup_styles()
        self._build_layout()
        self.show_chat()
        self._refresh_connection_state()
        self.root.after(120, self._poll_request_queue)

    def _setup_styles(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Gik.TCombobox",
                        fieldbackground=CARD_ALT,
                        background=CARD_ALT,
                        foreground=TEXT,
                        arrowcolor=GREEN,
                        bordercolor=BORDER,
                        lightcolor=BORDER,
                        darkcolor=BORDER,
                        padding=9)
        style.map("Gik.TCombobox",
                  fieldbackground=[("readonly", CARD_ALT)],
                  foreground=[("readonly", TEXT)],
                  selectbackground=[("readonly", GREEN_DARK)],
                  selectforeground=[("readonly", TEXT)])

    def _build_layout(self) -> None:
        shell = tk.Frame(self.root, bg=BG)
        shell.pack(fill="both", expand=True)

        self.sidebar = tk.Frame(shell, bg=SIDEBAR, width=238)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)

        brand = tk.Frame(self.sidebar, bg=SIDEBAR)
        brand.pack(fill="x", padx=22, pady=(24, 26))
        logo = tk.Canvas(brand, width=43, height=43, bg=SIDEBAR, highlightthickness=0)
        logo.pack(side="left")
        logo.create_oval(2, 2, 41, 41, fill=GREEN, outline="")
        logo.create_text(21, 21, text="G", fill="#06251A", font=("Segoe UI", 22, "bold"))
        brand_text = tk.Frame(brand, bg=SIDEBAR)
        brand_text.pack(side="left", padx=(11, 0))
        tk.Label(brand_text, text="GikAI", bg=SIDEBAR, fg=TEXT,
                 font=("Segoe UI", 19, "bold")).pack(anchor="w")
        tk.Label(brand_text, text="DESKTOP", bg=SIDEBAR, fg=MUTED,
                 font=("Segoe UI", 8, "bold")).pack(anchor="w")

        self._sidebar_button("＋  Новый чат", self.new_chat, primary=True).pack(fill="x", padx=16, pady=(0, 18))
        self._side_label("РАБОЧЕЕ ПРОСТРАНСТВО").pack(fill="x", padx=22, pady=(4, 8))
        self.chat_nav = self._sidebar_button("◉  Чат", self.show_chat)
        self.chat_nav.pack(fill="x", padx=12, pady=3)
        self.settings_nav = self._sidebar_button("⚙  Настройки API", self.show_settings)
        self.settings_nav.pack(fill="x", padx=12, pady=3)
        self.about_nav = self._sidebar_button("ⓘ  О приложении", self.show_about)
        self.about_nav.pack(fill="x", padx=12, pady=3)

        bottom = tk.Frame(self.sidebar, bg=SIDEBAR)
        bottom.pack(side="bottom", fill="x", padx=16, pady=18)
        tk.Frame(bottom, bg=BORDER, height=1).pack(fill="x", pady=(0, 14))
        tk.Label(bottom, text="СОСТОЯНИЕ", bg=SIDEBAR, fg=MUTED,
                 font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=5)
        self.sidebar_state = tk.Label(bottom, text="●  ИИ не подключён", bg=SIDEBAR,
                                      fg="#E5C27A", font=("Segoe UI", 9))
        self.sidebar_state.pack(anchor="w", padx=5, pady=(7, 3))
        tk.Label(bottom, text="Твой личный ИИ", bg=SIDEBAR, fg=MUTED,
                 font=("Segoe UI", 9)).pack(anchor="w", padx=5)

        self.content = tk.Frame(shell, bg=BG)
        self.content.pack(side="left", fill="both", expand=True)

        header = tk.Frame(self.content, bg=BG, height=72)
        header.pack(fill="x", padx=30, pady=(18, 8))
        header.pack_propagate(False)
        self.page_title = tk.Label(header, text="Чат", bg=BG, fg=TEXT,
                                   font=("Segoe UI", 18, "bold"))
        self.page_title.pack(side="left", anchor="center")
        self.header_status = tk.Label(header, text="●  ДЕМО-РЕЖИМ", bg="#263126", fg="#C9D99A",
                                      font=("Segoe UI", 8, "bold"), padx=12, pady=8)
        self.header_status.pack(side="right", anchor="center")

        self.page_host = tk.Frame(self.content, bg=BG)
        self.page_host.pack(fill="both", expand=True, padx=30, pady=(0, 24))

    def _side_label(self, text: str) -> tk.Label:
        return tk.Label(self.sidebar, text=text, bg=SIDEBAR, fg=MUTED,
                        font=("Segoe UI", 8, "bold"))

    def _sidebar_button(self, text: str, command, primary: bool = False) -> tk.Button:
        if primary:
            return tk.Button(self.sidebar, text=text, command=command,
                             bg=GREEN, fg="#06251A", activebackground="#55E2AE",
                             activeforeground="#06251A", relief="flat", bd=0,
                             font=("Segoe UI", 10, "bold"), cursor="hand2",
                             padx=12, pady=12)
        return tk.Button(self.sidebar, text=text, command=command,
                         bg=SIDEBAR, fg=MUTED, activebackground=CARD,
                         activeforeground=TEXT, relief="flat", bd=0,
                         font=("Segoe UI", 10), cursor="hand2",
                         anchor="w", padx=12, pady=11)

    def _clear_page(self) -> None:
        for child in self.page_host.winfo_children():
            child.destroy()

    def _select_nav(self, page: str) -> None:
        buttons = {"chat": self.chat_nav, "settings": self.settings_nav, "about": self.about_nav}
        for name, button in buttons.items():
            button.configure(bg=CARD if name == page else SIDEBAR,
                             fg=TEXT if name == page else MUTED)

    def show_chat(self) -> None:
        self.current_page = "chat"
        self.page_title.configure(text="Чат")
        self._select_nav("chat")
        self._clear_page()

        outer = tk.Frame(self.page_host, bg=BG)
        outer.pack(fill="both", expand=True)

        history_wrap = tk.Frame(outer, bg=BG)
        history_wrap.pack(fill="both", expand=True)
        self.chat_canvas = tk.Canvas(history_wrap, bg=BG, highlightthickness=0, bd=0)
        scrollbar = ttk.Scrollbar(history_wrap, orient="vertical", command=self.chat_canvas.yview)
        self.chat_canvas.configure(yscrollcommand=scrollbar.set)
        self.chat_canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.chat_inner = tk.Frame(self.chat_canvas, bg=BG)
        self.chat_window = self.chat_canvas.create_window((0, 0), window=self.chat_inner, anchor="nw")
        self.chat_inner.bind("<Configure>", self._on_chat_configure)
        self.chat_canvas.bind("<Configure>", self._on_canvas_configure)
        self.chat_canvas.bind_all("<MouseWheel>", self._on_mousewheel)

        if not self.chat_messages:
            self._build_welcome()
        else:
            for role, text in self.chat_messages:
                self._render_message(role, text)

        composer = tk.Frame(outer, bg=PANEL, highlightbackground=BORDER,
                            highlightthickness=1)
        composer.pack(fill="x", pady=(16, 0))
        top_line = tk.Frame(composer, bg=PANEL)
        top_line.pack(fill="x", padx=17, pady=(12, 0))
        tk.Label(top_line, text="Сообщение для GikAI", bg=PANEL, fg=MUTED,
                 font=("Segoe UI", 9)).pack(side="left")
        self.demo_note = tk.Label(top_line, text=self._chat_mode_note(),
                                  bg=PANEL, fg="#C9D99A", font=("Segoe UI", 8))
        self.demo_note.pack(side="right")

        input_row = tk.Frame(composer, bg=PANEL)
        input_row.pack(fill="x", padx=12, pady=10)
        self.message_entry = tk.Entry(input_row, bg=CARD_ALT, fg=TEXT,
                                      insertbackground=GREEN, relief="flat", bd=0,
                                      font=("Segoe UI", 11))
        self.message_entry.pack(side="left", fill="x", expand=True, ipady=13, padx=(0, 10))
        self.message_entry.insert(0, "Напиши сообщение...")
        self.message_entry.configure(fg=MUTED)
        self.message_entry.bind("<FocusIn>", self._clear_placeholder)
        self.message_entry.bind("<Return>", self._send_message)
        self.send_button = tk.Button(input_row, text="Отправить  ➤", command=self._send_message,
                             bg=GREEN, fg="#06251A", activebackground="#55E2AE",
                             activeforeground="#06251A", relief="flat", bd=0,
                             font=("Segoe UI", 9, "bold"), cursor="hand2",
                             padx=18, pady=11)
        self.send_button.pack(side="right")

        tk.Label(outer, text="Не вводи сюда секретные ключи. Для них есть отдельный раздел «Настройки API».",
                 bg=BG, fg=MUTED, font=("Segoe UI", 8)).pack(anchor="center", pady=(8, 0))

    def _build_welcome(self) -> None:
        welcome = tk.Frame(self.chat_inner, bg=BG)
        welcome.pack(fill="both", expand=True, pady=(75, 65))
        logo = tk.Canvas(welcome, width=76, height=76, bg=BG, highlightthickness=0)
        logo.pack()
        logo.create_oval(3, 3, 73, 73, fill=GREEN, outline="")
        logo.create_text(38, 38, text="G", fill="#06251A", font=("Segoe UI", 38, "bold"))
        tk.Label(welcome, text="Привет! Я GikAI", bg=BG, fg=TEXT,
                 font=("Segoe UI", 24, "bold")).pack(pady=(18, 7))
        tk.Label(welcome, text="Твоё личное пространство для общения с искусственным интеллектом.",
                 bg=BG, fg=MUTED, font=("Segoe UI", 10)).pack()
        state_card = tk.Frame(welcome, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        state_card.pack(pady=26, ipadx=22, ipady=16)
        tk.Label(state_card, text="ГОТОВ К ПОДКЛЮЧЕНИЮ ТВОЕГО API", bg=CARD, fg="#C9D99A",
                 font=("Segoe UI", 8, "bold")).pack(pady=(0, 7))
        tk.Label(state_card, text="Добавь ключ, проверь соединение и начинай общаться.",
                 bg=CARD, fg=TEXT, font=("Segoe UI", 10)).pack()
        settings_btn = tk.Button(welcome, text="Открыть настройки API  →", command=self.show_settings,
                                 bg=GREEN_DARK, fg=TEXT, activebackground="#287F5E",
                                 activeforeground=TEXT, relief="flat", bd=0,
                                 font=("Segoe UI", 9, "bold"), cursor="hand2",
                                 padx=18, pady=11)
        settings_btn.pack()

    def _on_chat_configure(self, _event=None) -> None:
        self.chat_canvas.configure(scrollregion=self.chat_canvas.bbox("all"))

    def _on_canvas_configure(self, event) -> None:
        self.chat_canvas.itemconfigure(self.chat_window, width=event.width)

    def _on_mousewheel(self, event) -> None:
        if self.current_page == "chat" and hasattr(self, "chat_canvas"):
            self.chat_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _clear_placeholder(self, _event=None) -> None:
        if self.message_entry.get() == "Напиши сообщение...":
            self.message_entry.delete(0, "end")
            self.message_entry.configure(fg=TEXT)

    def _send_message(self, _event=None) -> str:
        if self.request_in_progress or self.test_in_progress:
            return "break"
        message = self.message_entry.get().strip()
        if not message or message == "Напиши сообщение...":
            return "break"

        if not self.api_key_saved:
            messagebox.showinfo("GikAI", "Сначала добавь API-ключ в разделе «Настройки API».")
            self.show_settings()
            return "break"

        provider = self.config.get("provider", "Совместимый API")
        model = self.config.get("model", "").strip()
        base_url = self.config.get("base_url", "").strip()
        if not model:
            messagebox.showwarning("GikAI", "Укажи название модели в настройках API.")
            self.show_settings()
            return "break"
        if provider != "Google Gemini" and not base_url:
            messagebox.showwarning("GikAI", "Укажи Base URL в настройках API.")
            self.show_settings()
            return "break"

        if keyring is None:
            messagebox.showerror("GikAI", "Пакет keyring недоступен. Переустанови зависимости приложения.")
            return "break"
        try:
            api_key = keyring.get_password(SERVICE_NAME, "api_key")
        except Exception:
            api_key = None
        if not api_key:
            self.api_key_saved = False
            self._refresh_connection_state()
            messagebox.showwarning("GikAI", "Не удалось получить сохранённый API-ключ. Добавь его заново в настройках.")
            self.show_settings()
            return "break"

        self.chat_messages.append(("user", message))
        self.message_entry.delete(0, "end")
        self.message_entry.configure(fg=TEXT)
        self._render_message("user", message)
        self.chat_canvas.update_idletasks()
        self.chat_canvas.yview_moveto(1.0)

        history = list(self.chat_messages)
        session_id = self.chat_session_id
        settings = {"provider": provider, "model": model, "base_url": base_url, "api_key": api_key}
        self.request_in_progress = True
        self.send_button.configure(state="disabled", text="Думаю...")
        self.demo_note.configure(text="Запрос отправляется в API...")
        self.header_status.configure(text="●  ОТПРАВЛЯЮ ЗАПРОС", bg="#263126", fg="#C9D99A")
        self.sidebar_state.configure(text="●  ИИ отвечает...", fg="#C9D99A")

        worker = threading.Thread(
            target=self._request_worker,
            args=(session_id, settings, history),
            daemon=True,
        )
        worker.start()
        return "break"

    def _request_worker(self, session_id: int, settings: dict, history: list[tuple[str, str]]) -> None:
        try:
            answer = request_model(settings, history)
            self.request_queue.put((session_id, "success", answer))
        except Exception as exc:
            self.request_queue.put((session_id, "error", str(exc)))

    def _poll_request_queue(self) -> None:
        try:
            while True:
                session_id, result_type, payload = self.request_queue.get_nowait()
                if result_type in {"test_success", "test_error"}:
                    self.test_in_progress = False
                    if hasattr(self, "test_api_button") and self.test_api_button.winfo_exists():
                        self.test_api_button.configure(state="normal", text="Проверить API")
                    self._refresh_connection_state()
                    if result_type == "test_success":
                        messagebox.showinfo("Проверка GikAI", "API отвечает!\n\nТестовый ответ модели:\n" + payload[:1200])
                    else:
                        messagebox.showerror("Проверка GikAI", "Не удалось связаться с API:\n\n" + payload[:1200])
                    continue

                self.request_in_progress = False
                if hasattr(self, "send_button") and self.send_button.winfo_exists():
                    self.send_button.configure(state="normal", text="Отправить  ➤")
                self._refresh_connection_state()
                if session_id == self.chat_session_id:
                    if result_type == "success":
                        self.chat_messages.append(("assistant", payload))
                    else:
                        self.chat_messages.append(("assistant", f"Не удалось получить ответ.\n\n{payload}"))
                    if self.current_page == "chat":
                        self.show_chat()
        except queue.Empty:
            pass
        finally:
            try:
                self.root.after(120, self._poll_request_queue)
            except tk.TclError:
                pass

    def _chat_mode_note(self) -> str:
        if self.api_key_saved and self.config.get("model"):
            return "API подключён — можно отправлять сообщения"
        return "Добавь API-ключ и модель в настройках"

    def new_chat(self) -> None:
        self.chat_session_id += 1
        self.chat_messages = []
        self.show_chat()

    def _render_message(self, role: str, text: str) -> None:
        row = tk.Frame(self.chat_inner, bg=BG)
        row.pack(fill="x", padx=18, pady=8)
        is_user = role == "user"
        bubble = tk.Frame(row, bg="#1D4A3A" if is_user else CARD,
                          highlightbackground="#28664F" if is_user else BORDER,
                          highlightthickness=1)
        bubble.pack(side="right" if is_user else "left", padx=(90, 0) if is_user else (0, 90))
        tk.Label(bubble, text="Ты" if is_user else "GikAI",
                 bg=bubble["bg"], fg=GREEN if not is_user else "#B9F2D8",
                 font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=13, pady=(9, 2))
        message_label = tk.Label(bubble, text=text, bg=bubble["bg"], fg=TEXT,
                                 font=("Segoe UI", 10), justify="left", wraplength=620,
                                 padx=13, pady=0)
        message_label.pack(anchor="w", pady=(0, 11))

    def show_settings(self) -> None:
        self.current_page = "settings"
        self.page_title.configure(text="Настройки API")
        self._select_nav("settings")
        self._clear_page()

        wrapper = tk.Frame(self.page_host, bg=BG)
        wrapper.pack(fill="both", expand=True)
        tk.Label(wrapper, text="Подключение своего ключа", bg=BG, fg=TEXT,
                 font=("Segoe UI", 20, "bold")).pack(anchor="w", pady=(6, 5))
        tk.Label(wrapper, text="Укажи провайдера, точный ID модели и свой API-ключ. После сохранения нажми «Проверить API», чтобы убедиться, что подключение работает.",
                 bg=BG, fg=MUTED, font=("Segoe UI", 10)).pack(anchor="w", pady=(0, 18))

        card = tk.Frame(wrapper, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        card.pack(fill="x", anchor="n")
        form = tk.Frame(card, bg=PANEL)
        form.pack(fill="x", padx=22, pady=20)

        self.provider_var = tk.StringVar(value=self.config.get("provider", "Совместимый API"))
        self.model_var = tk.StringVar(value=self.config.get("model", ""))
        self.base_url_var = tk.StringVar(value=self.config.get("base_url", ""))
        self.key_var = tk.StringVar(value="")

        self._field_label(form, "ПРОВАЙДЕР", 0)
        provider_box = ttk.Combobox(form, textvariable=self.provider_var,
                                    values=["OpenAI", "OpenRouter", "Google Gemini", "Совместимый API", "Другой"],
                                    state="readonly", style="Gik.TCombobox", font=("Segoe UI", 10))
        provider_box.grid(row=1, column=0, sticky="ew", pady=(6, 15))
        provider_box.bind("<<ComboboxSelected>>", self._provider_changed)

        self._field_label(form, "НАЗВАНИЕ МОДЕЛИ (ТОЧНЫЙ ID ИЗ ПРОВАЙДЕРА)", 2)
        model_entry = self._entry(form, self.model_var, 3)
        model_entry.configure(insertbackground=GREEN)

        self._field_label(form, "BASE URL (НЕОБЯЗАТЕЛЬНО)", 4)
        url_entry = self._entry(form, self.base_url_var, 5)
        url_entry.configure(insertbackground=GREEN)

        self._field_label(form, "API-КЛЮЧ", 6)
        key_row = tk.Frame(form, bg=PANEL)
        key_row.grid(row=7, column=0, sticky="ew", pady=(6, 8))
        self.key_entry = tk.Entry(key_row, textvariable=self.key_var, show="●", bg=CARD_ALT,
                                  fg=TEXT, insertbackground=GREEN, relief="flat", bd=0,
                                  font=("Segoe UI", 10))
        self.key_entry.pack(side="left", fill="x", expand=True, ipady=11, padx=(0, 8))
        self.show_key = False
        self.show_key_btn = tk.Button(key_row, text="Показать", command=self._toggle_key,
                                      bg=CARD_ALT, fg=MUTED, activebackground=CARD,
                                      activeforeground=TEXT, relief="flat", bd=0,
                                      font=("Segoe UI", 8), cursor="hand2", padx=12, pady=10)
        self.show_key_btn.pack(side="right")

        self.key_status = tk.Label(form, text=self._key_status_text(), bg=PANEL,
                                   fg=GREEN if self.api_key_saved else MUTED,
                                   font=("Segoe UI", 9))
        self.key_status.grid(row=8, column=0, sticky="w", pady=(0, 16))

        buttons = tk.Frame(form, bg=PANEL)
        buttons.grid(row=9, column=0, sticky="w")
        tk.Button(buttons, text="Сохранить настройки", command=self._save_settings,
                  bg=GREEN, fg="#06251A", activebackground="#55E2AE",
                  activeforeground="#06251A", relief="flat", bd=0,
                  font=("Segoe UI", 9, "bold"), cursor="hand2",
                  padx=18, pady=11).pack(side="left")
        self.test_api_button = tk.Button(buttons, text="Проверить API", command=self._test_api,
                                         bg=CARD_ALT, fg=GREEN, activebackground=CARD,
                                         activeforeground=TEXT, relief="flat", bd=0,
                                         font=("Segoe UI", 9, "bold"), cursor="hand2",
                                         padx=14, pady=11)
        self.test_api_button.pack(side="left", padx=(9, 0))
        tk.Button(buttons, text="Удалить сохранённый ключ", command=self._delete_key,
                  bg=CARD_ALT, fg=TEXT, activebackground=CARD,
                  activeforeground=TEXT, relief="flat", bd=0,
                  font=("Segoe UI", 9), cursor="hand2",
                  padx=14, pady=11).pack(side="left", padx=(9, 0))

        form.grid_columnconfigure(0, weight=1)
        note = tk.Frame(wrapper, bg="#17271F", highlightbackground="#2E624B", highlightthickness=1)
        note.pack(fill="x", pady=16)
        tk.Label(note, text="🔒  БЕЗОПАСНОСТЬ", bg="#17271F", fg="#B9F2D8",
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=16, pady=(12, 4))
        tk.Label(note, text="API-ключ не записывается в файл настроек: он сохраняется в хранилище учётных данных Windows. Запросы могут расходовать лимиты или деньги на стороне провайдера — перед использованием проверь условия своего API-аккаунта.",
                 bg="#17271F", fg="#D2E7DC", justify="left", wraplength=760,
                 font=("Segoe UI", 9)).pack(anchor="w", padx=16, pady=(0, 13))

    def _field_label(self, parent, text: str, row: int) -> None:
        tk.Label(parent, text=text, bg=PANEL, fg=MUTED,
                 font=("Segoe UI", 8, "bold")).grid(row=row, column=0, sticky="w")

    def _entry(self, parent, variable: tk.StringVar, row: int) -> tk.Entry:
        entry = tk.Entry(parent, textvariable=variable, bg=CARD_ALT, fg=TEXT,
                         insertbackground=GREEN, relief="flat", bd=0,
                         font=("Segoe UI", 10))
        entry.grid(row=row, column=0, sticky="ew", pady=(6, 15), ipady=11)
        return entry

    def _provider_changed(self, _event=None) -> None:
        provider = self.provider_var.get()
        defaults = {
            "OpenAI": "https://api.openai.com/v1",
            "OpenRouter": "https://openrouter.ai/api/v1",
            "Google Gemini": "https://generativelanguage.googleapis.com/v1beta",
        }
        if provider in defaults:
            self.base_url_var.set(defaults[provider])
            if not self.model_var.get().strip():
                self.model_var.set("")

    def _toggle_key(self) -> None:
        self.show_key = not self.show_key
        self.key_entry.configure(show="" if self.show_key else "●")
        self.show_key_btn.configure(text="Скрыть" if self.show_key else "Показать")

    def _has_saved_key(self) -> bool:
        if keyring is None:
            return False
        try:
            return bool(keyring.get_password(SERVICE_NAME, "api_key"))
        except Exception:
            return False

    def _key_status_text(self) -> str:
        if self.api_key_saved:
            return "●  API-ключ уже сохранён в хранилище Windows. Повторно вводить его не нужно."
        if keyring is None:
            return "Хранилище ключей недоступно: проверь установку пакета keyring."
        return "○  API-ключ пока не сохранён."

    def _save_settings(self) -> None:
        self.config["provider"] = self.provider_var.get().strip()
        self.config["model"] = self.model_var.get().strip()
        self.config["base_url"] = self.base_url_var.get().strip()
        try:
            save_config(self.config)
        except OSError as exc:
            messagebox.showerror("GikAI", f"Не удалось сохранить настройки:\n{exc}")
            return

        key = self.key_var.get().strip()
        if key:
            if keyring is None:
                messagebox.showerror("GikAI", "Пакет keyring не установлен. Ключ не сохранён.")
                return
            try:
                keyring.set_password(SERVICE_NAME, "api_key", key)
                self.api_key_saved = True
                self.key_var.set("")
                self.key_status.configure(text=self._key_status_text(), fg=GREEN)
            except Exception as exc:
                messagebox.showerror("Не удалось сохранить ключ",
                                     "Windows не позволила сохранить ключ в защищённом хранилище.\n\n"
                                     f"Подробности: {exc}\n\nКлюч не был записан в файл настроек.")
                return
        self._refresh_connection_state()
        if hasattr(self, "demo_note") and self.demo_note.winfo_exists():
            self.demo_note.configure(text=self._chat_mode_note())
        messagebox.showinfo("GikAI", "Настройки сохранены. Теперь можно отправить тестовое сообщение в чате.")

    def _test_api(self) -> None:
        if self.request_in_progress or self.test_in_progress:
            messagebox.showinfo("GikAI", "Сначала дождись завершения текущего запроса.")
            return

        provider = self.provider_var.get().strip()
        model = self.model_var.get().strip()
        base_url = self.base_url_var.get().strip()
        if not model:
            messagebox.showwarning("GikAI", "Укажи точный ID модели.")
            return
        if provider != "Google Gemini" and not base_url:
            messagebox.showwarning("GikAI", "Укажи Base URL API.")
            return
        if keyring is None:
            messagebox.showerror("GikAI", "Хранилище API-ключей недоступно. Проверь установку зависимостей.")
            return
        try:
            api_key = keyring.get_password(SERVICE_NAME, "api_key")
        except Exception:
            api_key = None
        if not api_key:
            messagebox.showwarning("GikAI", "Сначала сохрани API-ключ.")
            return

        settings = {"provider": provider, "model": model, "base_url": base_url, "api_key": api_key}
        self.test_in_progress = True
        self.test_api_button.configure(state="disabled", text="Проверяем...")
        worker = threading.Thread(target=self._test_api_worker, args=(self.chat_session_id, settings), daemon=True)
        worker.start()

    def _test_api_worker(self, session_id: int, settings: dict) -> None:
        try:
            answer = request_model(settings, [("user", "Ответь одной короткой фразой: GikAI подключён.")])
            self.request_queue.put((session_id, "test_success", answer))
        except Exception as exc:
            self.request_queue.put((session_id, "test_error", str(exc)))

    def _delete_key(self) -> None:
        if keyring is None:
            messagebox.showwarning("GikAI", "Хранилище ключей недоступно.")
            return
        try:
            keyring.delete_password(SERVICE_NAME, "api_key")
        except keyring.errors.PasswordDeleteError:
            pass
        except Exception as exc:
            messagebox.showerror("GikAI", f"Не удалось удалить ключ:\n{exc}")
            return
        self.api_key_saved = False
        self.key_var.set("")
        self.key_status.configure(text=self._key_status_text(), fg=MUTED)
        self._refresh_connection_state()
        if hasattr(self, "demo_note") and self.demo_note.winfo_exists():
            self.demo_note.configure(text=self._chat_mode_note())
        messagebox.showinfo("GikAI", "Сохранённый API-ключ удалён.")

    def _refresh_connection_state(self) -> None:
        configured = self.api_key_saved and bool(self.config.get("model", "").strip())
        if configured:
            self.sidebar_state.configure(text="●  API настроен", fg=GREEN)
            self.header_status.configure(text="●  API ГОТОВ", bg="#173A2B", fg="#8CE7BC")
        else:
            self.sidebar_state.configure(text="●  ИИ не подключён", fg="#E5C27A")
            self.header_status.configure(text="●  НЕ НАСТРОЕН", bg="#263126", fg="#C9D99A")

    def show_about(self) -> None:
        self.current_page = "about"
        self.page_title.configure(text="О приложении")
        self._select_nav("about")
        self._clear_page()
        card = tk.Frame(self.page_host, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        card.pack(fill="x", anchor="n", pady=8)
        inner = tk.Frame(card, bg=PANEL)
        inner.pack(fill="x", padx=24, pady=24)
        tk.Label(inner, text="GikAI Desktop", bg=PANEL, fg=TEXT,
                 font=("Segoe UI", 21, "bold")).pack(anchor="w")
        tk.Label(inner, text="Версия 0.3.1 · исправлено отображение сообщений", bg=PANEL, fg=GREEN,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(4, 18))
        tk.Label(inner, text="GikAI Desktop поддерживает текстовый чат через OpenAI-совместимые API (в том числе OpenAI и OpenRouter), а также Google Gemini. История диалога хранится только в памяти приложения; после закрытия она очищается. Генерация изображений и постоянная память будут добавлены отдельно.",
                 bg=PANEL, fg=MUTED, justify="left", wraplength=760,
                 font=("Segoe UI", 10)).pack(anchor="w")


def main() -> None:
    root = tk.Tk()
    try:
        app = GikAIApp(root)
    except Exception as exc:
        messagebox.showerror("GikAI — ошибка запуска", str(exc))
        raise
    root.mainloop()


if __name__ == "__main__":
    main()
