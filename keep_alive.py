from flask import Flask
from threading import Thread
import logging

# Убираем лишние логи, чтобы не спамило
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

app = Flask('')


@app.route('/')
def home():
    return "I'm alive! Bot is running."


def run():
    try:
        app.run(host='0.0.0.0', port=8080)
    except Exception as e:
        print(f"Ошибка сервера: {e}")


def keep_alive():
    t = Thread(target=run)
    t.start()
